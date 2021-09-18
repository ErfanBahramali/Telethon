import inspect
import itertools
import typing
import warnings

from .. import errors, hints, _tl
from .._misc import helpers, utils, requestiter
from ..types import _custom

_MAX_CHUNK_SIZE = 100

if typing.TYPE_CHECKING:
    from .telegramclient import TelegramClient


class _MessagesIter(requestiter.RequestIter):
    """
    Common factor for all requests that need to iterate over messages.
    """
    async def _init(
            self, entity, offset_id, min_id, max_id,
            from_user, offset_date, add_offset, filter, search, reply_to,
            scheduled
    ):
        # Note that entity being `None` will perform a global search.
        if entity:
            self.entity = await self.client.get_input_entity(entity)
        else:
            self.entity = None
            if self.reverse:
                raise ValueError('Cannot reverse global search')

        # Telegram doesn't like min_id/max_id. If these IDs are low enough
        # (starting from last_id - 100), the request will return nothing.
        #
        # We can emulate their behaviour locally by setting offset = max_id
        # and simply stopping once we hit a message with ID <= min_id.
        if self.reverse:
            offset_id = max(offset_id, min_id)
            if offset_id and max_id:
                if max_id - offset_id <= 1:
                    raise StopAsyncIteration

            if not max_id:
                max_id = float('inf')
        else:
            offset_id = max(offset_id, max_id)
            if offset_id and min_id:
                if offset_id - min_id <= 1:
                    raise StopAsyncIteration

        if self.reverse:
            if offset_id:
                offset_id += 1
            elif not offset_date:
                # offset_id has priority over offset_date, so don't
                # set offset_id to 1 if we want to offset by date.
                offset_id = 1

        if from_user:
            from_user = await self.client.get_input_entity(from_user)
            self.from_id = await self.client.get_peer_id(from_user)
        else:
            self.from_id = None

        # `messages.searchGlobal` only works with text `search` or `filter` queries.
        # If we want to perform global a search with `from_user` we have to perform
        # a normal `messages.search`, *but* we can make the entity be `inputPeerEmpty`.
        if not self.entity and from_user:
            self.entity = _tl.InputPeerEmpty()

        if filter is None:
            filter = _tl.InputMessagesFilterEmpty()
        else:
            filter = filter() if isinstance(filter, type) else filter

        if not self.entity:
            self.request = _tl.fn.messages.SearchGlobal(
                q=search or '',
                filter=filter,
                min_date=None,
                max_date=offset_date,
                offset_rate=0,
                offset_peer=_tl.InputPeerEmpty(),
                offset_id=offset_id,
                limit=1
            )
        elif scheduled:
            self.request = _tl.fn.messages.GetScheduledHistory(
                peer=entity,
                hash=0
            )
        elif reply_to is not None:
            self.request = _tl.fn.messages.GetReplies(
                peer=self.entity,
                msg_id=reply_to,
                offset_id=offset_id,
                offset_date=offset_date,
                add_offset=add_offset,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0
            )
        elif search is not None or not isinstance(filter, _tl.InputMessagesFilterEmpty) or from_user:
            # Telegram completely ignores `from_id` in private chats
            ty = helpers._entity_type(self.entity)
            if ty == helpers._EntityType.USER:
                # Don't bother sending `from_user` (it's ignored anyway),
                # but keep `from_id` defined above to check it locally.
                from_user = None
            else:
                # Do send `from_user` to do the filtering server-side,
                # and set `from_id` to None to avoid checking it locally.
                self.from_id = None

            self.request = _tl.fn.messages.Search(
                peer=self.entity,
                q=search or '',
                filter=filter,
                min_date=None,
                max_date=offset_date,
                offset_id=offset_id,
                add_offset=add_offset,
                limit=0,  # Search actually returns 0 items if we ask it to
                max_id=0,
                min_id=0,
                hash=0,
                from_id=from_user
            )

            # Workaround issue #1124 until a better solution is found.
            # Telegram seemingly ignores `max_date` if `filter` (and
            # nothing else) is specified, so we have to rely on doing
            # a first request to offset from the ID instead.
            #
            # Even better, using `filter` and `from_id` seems to always
            # trigger `RPC_CALL_FAIL` which is "internal issues"...
            if not isinstance(filter, _tl.InputMessagesFilterEmpty) \
                    and offset_date and not search and not offset_id:
                async for m in self.client.iter_messages(
                        self.entity, 1, offset_date=offset_date):
                    self.request.offset_id = m.id + 1
        else:
            self.request = _tl.fn.messages.GetHistory(
                peer=self.entity,
                limit=1,
                offset_date=offset_date,
                offset_id=offset_id,
                min_id=0,
                max_id=0,
                add_offset=add_offset,
                hash=0
            )

        if self.limit <= 0:
            # No messages, but we still need to know the total message count
            result = await self.client(self.request)
            if isinstance(result, _tl.messages.MessagesNotModified):
                self.total = result.count
            else:
                self.total = getattr(result, 'count', len(result.messages))
            raise StopAsyncIteration

        if self.wait_time is None:
            self.wait_time = 1 if self.limit > 3000 else 0

        # When going in reverse we need an offset of `-limit`, but we
        # also want to respect what the user passed, so add them together.
        if self.reverse:
            self.request.add_offset -= _MAX_CHUNK_SIZE

        self.add_offset = add_offset
        self.max_id = max_id
        self.min_id = min_id
        self.last_id = 0 if self.reverse else float('inf')

    async def _load_next_chunk(self):
        self.request.limit = min(self.left, _MAX_CHUNK_SIZE)
        if self.reverse and self.request.limit != _MAX_CHUNK_SIZE:
            # Remember that we need -limit when going in reverse
            self.request.add_offset = self.add_offset - self.request.limit

        r = await self.client(self.request)
        self.total = getattr(r, 'count', len(r.messages))

        entities = {utils.get_peer_id(x): x
                    for x in itertools.chain(r.users, r.chats)}

        messages = reversed(r.messages) if self.reverse else r.messages
        for message in messages:
            if (isinstance(message, _tl.MessageEmpty)
                    or self.from_id and message.sender_id != self.from_id):
                continue

            if not self._message_in_range(message):
                return True

            # There has been reports that on bad connections this method
            # was returning duplicated IDs sometimes. Using ``last_id``
            # is an attempt to avoid these duplicates, since the message
            # IDs are returned in descending order (or asc if reverse).
            self.last_id = message.id
            self.buffer.append(_custom.Message._new(self.client, message, entities, self.entity))

        if len(r.messages) < self.request.limit:
            return True

        # Get the last message that's not empty (in some rare cases
        # it can happen that the last message is :tl:`MessageEmpty`)
        if self.buffer:
            self._update_offset(self.buffer[-1], r)
        else:
            # There are some cases where all the messages we get start
            # being empty. This can happen on migrated mega-groups if
            # the history was cleared, and we're using search. Telegram
            # acts incredibly weird sometimes. Messages are returned but
            # only "empty", not their contents. If this is the case we
            # should just give up since there won't be any new Message.
            return True

    def _message_in_range(self, message):
        """
        Determine whether the given message is in the range or
        it should be ignored (and avoid loading more chunks).
        """
        # No entity means message IDs between chats may vary
        if self.entity:
            if self.reverse:
                if message.id <= self.last_id or message.id >= self.max_id:
                    return False
            else:
                if message.id >= self.last_id or message.id <= self.min_id:
                    return False

        return True

    def _update_offset(self, last_message, response):
        """
        After making the request, update its offset with the last message.
        """
        self.request.offset_id = last_message.id
        if self.reverse:
            # We want to skip the one we already have
            self.request.offset_id += 1

        if isinstance(self.request, _tl.fn.messages.Search):
            # Unlike getHistory and searchGlobal that use *offset* date,
            # this is *max* date. This means that doing a search in reverse
            # will break it. Since it's not really needed once we're going
            # (only for the first request), it's safe to just clear it off.
            self.request.max_date = None
        else:
            # getHistory, searchGlobal and getReplies call it offset_date
            self.request.offset_date = last_message.date

        if isinstance(self.request, _tl.fn.messages.SearchGlobal):
            if last_message.input_chat:
                self.request.offset_peer = last_message.input_chat
            else:
                self.request.offset_peer = _tl.InputPeerEmpty()

            self.request.offset_rate = getattr(response, 'next_rate', 0)


class _IDsIter(requestiter.RequestIter):
    async def _init(self, entity, ids):
        self.total = len(ids)
        self._ids = list(reversed(ids)) if self.reverse else ids
        self._offset = 0
        self._entity = (await self.client.get_input_entity(entity)) if entity else None
        self._ty = helpers._entity_type(self._entity) if self._entity else None

        # 30s flood wait every 300 messages (3 requests of 100 each, 30 of 10, etc.)
        if self.wait_time is None:
            self.wait_time = 10 if self.limit > 300 else 0

    async def _load_next_chunk(self):
        ids = self._ids[self._offset:self._offset + _MAX_CHUNK_SIZE]
        if not ids:
            raise StopAsyncIteration

        self._offset += _MAX_CHUNK_SIZE

        from_id = None  # By default, no need to validate from_id
        if self._ty == helpers._EntityType.CHANNEL:
            try:
                r = await self.client(
                    _tl.fn.channels.GetMessages(self._entity, ids))
            except errors.MessageIdsEmptyError:
                # All IDs were invalid, use a dummy result
                r = _tl.messages.MessagesNotModified(len(ids))
        else:
            r = await self.client(_tl.fn.messages.GetMessages(ids))
            if self._entity:
                from_id = await self.client._get_peer(self._entity)

        if isinstance(r, _tl.messages.MessagesNotModified):
            self.buffer.extend(None for _ in ids)
            return

        entities = {utils.get_peer_id(x): x
                    for x in itertools.chain(r.users, r.chats)}

        # Telegram seems to return the messages in the order in which
        # we asked them for, so we don't need to check it ourselves,
        # unless some messages were invalid in which case Telegram
        # may decide to not send them at all.
        #
        # The passed message IDs may not belong to the desired entity
        # since the user can enter arbitrary numbers which can belong to
        # arbitrary chats. Validate these unless ``from_id is None``.
        for message in r.messages:
            if isinstance(message, _tl.MessageEmpty) or (
                    from_id and message.peer_id != from_id):
                self.buffer.append(None)
            else:
                self.buffer.append(_custom.Message._new(self.client, message, entities, self._entity))


def get_messages(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        limit: float = None,
        *,
        offset_date: 'hints.DateLike' = None,
        offset_id: int = 0,
        max_id: int = 0,
        min_id: int = 0,
        add_offset: int = 0,
        search: str = None,
        filter: 'typing.Union[_tl.TypeMessagesFilter, typing.Type[_tl.TypeMessagesFilter]]' = None,
        from_user: 'hints.EntityLike' = None,
        wait_time: float = None,
        ids: 'typing.Union[int, typing.Sequence[int]]' = None,
        reverse: bool = False,
        reply_to: int = None,
        scheduled: bool = False
) -> 'typing.Union[_MessagesIter, _IDsIter]':
    if ids is not None:
        if not utils.is_list_like(ids):
            ids = [ids]

        return _IDsIter(
            client=self,
            reverse=reverse,
            wait_time=wait_time,
            limit=len(ids),
            entity=entity,
            ids=ids
        )

    return _MessagesIter(
        client=self,
        reverse=reverse,
        wait_time=wait_time,
        limit=limit,
        entity=entity,
        offset_id=offset_id,
        min_id=min_id,
        max_id=max_id,
        from_user=from_user,
        offset_date=offset_date,
        add_offset=add_offset,
        filter=filter,
        search=search,
        reply_to=reply_to,
        scheduled=scheduled
    )


async def _get_comment_data(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message: 'typing.Union[int, _tl.Message]'
):
    r = await self(_tl.fn.messages.GetDiscussionMessage(
        peer=entity,
        msg_id=utils.get_message_id(message)
    ))
    m = r.messages[0]
    chat = next(c for c in r.chats if c.id == m.peer_id.channel_id)
    return utils.get_input_peer(chat), m.id

async def send_message(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message: 'hints.MessageLike' = '',
        *,
        reply_to: 'typing.Union[int, _tl.Message]' = None,
        attributes: 'typing.Sequence[_tl.TypeDocumentAttribute]' = None,
        parse_mode: typing.Optional[str] = (),
        formatting_entities: typing.Optional[typing.List[_tl.TypeMessageEntity]] = None,
        link_preview: bool = True,
        file: 'typing.Union[hints.FileLike, typing.Sequence[hints.FileLike]]' = None,
        thumb: 'hints.FileLike' = None,
        force_document: bool = False,
        clear_draft: bool = False,
        buttons: 'hints.MarkupLike' = None,
        silent: bool = None,
        background: bool = None,
        supports_streaming: bool = False,
        schedule: 'hints.DateLike' = None,
        comment_to: 'typing.Union[int, _tl.Message]' = None
) -> '_tl.Message':
    if file is not None:
        return await self.send_file(
            entity, file, caption=message, reply_to=reply_to,
            attributes=attributes, parse_mode=parse_mode,
            force_document=force_document, thumb=thumb,
            buttons=buttons, clear_draft=clear_draft, silent=silent,
            schedule=schedule, supports_streaming=supports_streaming,
            formatting_entities=formatting_entities,
            comment_to=comment_to, background=background
        )

    entity = await self.get_input_entity(entity)
    if comment_to is not None:
        entity, reply_to = await _get_comment_data(self, entity, comment_to)

    if isinstance(message, _tl.Message):
        if buttons is None:
            markup = message.reply_markup
        else:
            markup = self.build_reply_markup(buttons)

        if silent is None:
            silent = message.silent

        if (message.media and not isinstance(
                message.media, _tl.MessageMediaWebPage)):
            return await self.send_file(
                entity,
                message.media,
                caption=message.message,
                silent=silent,
                background=background,
                reply_to=reply_to,
                buttons=markup,
                formatting_entities=message.entities,
                schedule=schedule
            )

        request = _tl.fn.messages.SendMessage(
            peer=entity,
            message=message.message or '',
            silent=silent,
            background=background,
            reply_to_msg_id=utils.get_message_id(reply_to),
            reply_markup=markup,
            entities=message.entities,
            clear_draft=clear_draft,
            no_webpage=not isinstance(
                message.media, _tl.MessageMediaWebPage),
            schedule_date=schedule
        )
        message = message.message
    else:
        if formatting_entities is None:
            message, formatting_entities = await self._parse_message_text(message, parse_mode)
        if not message:
            raise ValueError(
                'The message cannot be empty unless a file is provided'
            )

        request = _tl.fn.messages.SendMessage(
            peer=entity,
            message=message,
            entities=formatting_entities,
            no_webpage=not link_preview,
            reply_to_msg_id=utils.get_message_id(reply_to),
            clear_draft=clear_draft,
            silent=silent,
            background=background,
            reply_markup=self.build_reply_markup(buttons),
            schedule_date=schedule
        )

    result = await self(request)
    if isinstance(result, _tl.UpdateShortSentMessage):
        return _custom.Message._new(self, _tl.Message(
            id=result.id,
            peer_id=await self._get_peer(entity),
            message=message,
            date=result.date,
            out=result.out,
            media=result.media,
            entities=result.entities,
            reply_markup=request.reply_markup,
            ttl_period=result.ttl_period
        ), {}, entity)

    return self._get_response_message(request, result, entity)

async def forward_messages(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        messages: 'typing.Union[hints.MessageIDLike, typing.Sequence[hints.MessageIDLike]]',
        from_peer: 'hints.EntityLike' = None,
        *,
        background: bool = None,
        with_my_score: bool = None,
        silent: bool = None,
        as_album: bool = None,
        schedule: 'hints.DateLike' = None
) -> 'typing.Sequence[_tl.Message]':
    if as_album is not None:
        warnings.warn('the as_album argument is deprecated and no longer has any effect')

    single = not utils.is_list_like(messages)
    if single:
        messages = (messages,)

    entity = await self.get_input_entity(entity)

    if from_peer:
        from_peer = await self.get_input_entity(from_peer)
        from_peer_id = await self.get_peer_id(from_peer)
    else:
        from_peer_id = None

    def get_key(m):
        if isinstance(m, int):
            if from_peer_id is not None:
                return from_peer_id

            raise ValueError('from_peer must be given if integer IDs are used')
        elif isinstance(m, _tl.Message):
            return m.chat_id
        else:
            raise TypeError('Cannot forward messages of type {}'.format(type(m)))

    sent = []
    for _chat_id, chunk in itertools.groupby(messages, key=get_key):
        chunk = list(chunk)
        if isinstance(chunk[0], int):
            chat = from_peer
        else:
            chat = await chunk[0].get_input_chat()
            chunk = [m.id for m in chunk]

        req = _tl.fn.messages.ForwardMessages(
            from_peer=chat,
            id=chunk,
            to_peer=entity,
            silent=silent,
            background=background,
            with_my_score=with_my_score,
            schedule_date=schedule
        )
        result = await self(req)
        sent.extend(self._get_response_message(req, result, entity))

    return sent[0] if single else sent

async def edit_message(
        self: 'TelegramClient',
        entity: 'typing.Union[hints.EntityLike, _tl.Message]',
        message: 'hints.MessageLike' = None,
        text: str = None,
        *,
        parse_mode: str = (),
        attributes: 'typing.Sequence[_tl.TypeDocumentAttribute]' = None,
        formatting_entities: typing.Optional[typing.List[_tl.TypeMessageEntity]] = None,
        link_preview: bool = True,
        file: 'hints.FileLike' = None,
        thumb: 'hints.FileLike' = None,
        force_document: bool = False,
        buttons: 'hints.MarkupLike' = None,
        supports_streaming: bool = False,
        schedule: 'hints.DateLike' = None
) -> '_tl.Message':
    if formatting_entities is None:
        text, formatting_entities = await self._parse_message_text(text, parse_mode)
    file_handle, media, image = await self._file_to_media(file,
            supports_streaming=supports_streaming,
            thumb=thumb,
            attributes=attributes,
            force_document=force_document)

    if isinstance(message, _tl.InputBotInlineMessageID):
        request = _tl.fn.messages.EditInlineBotMessage(
            id=message,
            message=text,
            no_webpage=not link_preview,
            entities=formatting_entities,
            media=media,
            reply_markup=self.build_reply_markup(buttons)
        )
        # Invoke `messages.editInlineBotMessage` from the right datacenter.
        # Otherwise, Telegram will error with `MESSAGE_ID_INVALID` and do nothing.
        exported = self.session.dc_id != entity.dc_id
        if exported:
            try:
                sender = await self._borrow_exported_sender(entity.dc_id)
                return await self._call(sender, request)
            finally:
                await self._return_exported_sender(sender)
        else:
            return await self(request)

    entity = await self.get_input_entity(entity)
    request = _tl.fn.messages.EditMessage(
        peer=entity,
        id=utils.get_message_id(message),
        message=text,
        no_webpage=not link_preview,
        entities=formatting_entities,
        media=media,
        reply_markup=self.build_reply_markup(buttons),
        schedule_date=schedule
    )
    msg = self._get_response_message(request, await self(request), entity)
    return msg

async def delete_messages(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message_ids: 'typing.Union[hints.MessageIDLike, typing.Sequence[hints.MessageIDLike]]',
        *,
        revoke: bool = True) -> 'typing.Sequence[_tl.messages.AffectedMessages]':
    if not utils.is_list_like(message_ids):
        message_ids = (message_ids,)

    message_ids = (
        m.id if isinstance(m, (
            _tl.Message, _tl.MessageService, _tl.MessageEmpty))
        else int(m) for m in message_ids
    )

    if entity:
        entity = await self.get_input_entity(entity)
        ty = helpers._entity_type(entity)
    else:
        # no entity (None), set a value that's not a channel for private delete
        ty = helpers._EntityType.USER

    if ty == helpers._EntityType.CHANNEL:
        res = await self([_tl.fn.channels.DeleteMessages(
                entity, list(c)) for c in utils.chunks(message_ids)])
    else:
        res = await self([_tl.fn.messages.DeleteMessages(
            list(c), revoke) for c in utils.chunks(message_ids)])

    return sum(r.pts_count for r in res)

async def send_read_acknowledge(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message: 'typing.Union[hints.MessageIDLike, typing.Sequence[hints.MessageIDLike]]' = None,
        *,
        max_id: int = None,
        clear_mentions: bool = False) -> bool:
    if max_id is None:
        if not message:
            max_id = 0
        else:
            if utils.is_list_like(message):
                max_id = max(msg.id for msg in message)
            else:
                max_id = message.id

    entity = await self.get_input_entity(entity)
    if clear_mentions:
        await self(_tl.fn.messages.ReadMentions(entity))
        if max_id is None:
            return True

    if max_id is not None:
        if helpers._entity_type(entity) == helpers._EntityType.CHANNEL:
            return await self(_tl.fn.channels.ReadHistory(
                utils.get_input_channel(entity), max_id=max_id))
        else:
            return await self(_tl.fn.messages.ReadHistory(
                entity, max_id=max_id))

    return False

async def pin_message(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message: 'typing.Optional[hints.MessageIDLike]',
        *,
        notify: bool = False,
        pm_oneside: bool = False
):
    return await _pin(self, entity, message, unpin=False, notify=notify, pm_oneside=pm_oneside)

async def unpin_message(
        self: 'TelegramClient',
        entity: 'hints.EntityLike',
        message: 'typing.Optional[hints.MessageIDLike]' = None,
        *,
        notify: bool = False
):
    return await _pin(self, entity, message, unpin=True, notify=notify)

async def _pin(self, entity, message, *, unpin, notify=False, pm_oneside=False):
    message = utils.get_message_id(message) or 0
    entity = await self.get_input_entity(entity)
    if message <= 0:  # old behaviour accepted negative IDs to unpin
        await self(_tl.fn.messages.UnpinAllMessages(entity))
        return

    request = _tl.fn.messages.UpdatePinnedMessage(
        peer=entity,
        id=message,
        silent=not notify,
        unpin=unpin,
        pm_oneside=pm_oneside
    )
    result = await self(request)

    # Unpinning does not produce a service message.
    # Pinning a message that was already pinned also produces no service message.
    # Pinning a message in your own chat does not produce a service message,
    # but pinning on a private conversation with someone else does.
    if unpin or not result.updates:
        return

    # Pinning a message that doesn't exist would RPC-error earlier
    return self._get_response_message(request, result, entity)