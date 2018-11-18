
from typing import overload, Tuple, Optional, FrozenSet, Iterable, \
    Sequence, List

from pymap.concurrent import Event, TimeoutError
from pymap.exceptions import MailboxNotFound, MailboxReadOnly
from pymap.flags import FlagOp
from pymap.listtree import ListTree
from pymap.mailbox import MailboxSnapshot
from pymap.message import AppendMessage
from pymap.parsing.specials import SequenceSet, FetchAttribute, SearchKey
from pymap.parsing.specials.flag import Flag, Deleted, Seen
from pymap.parsing.response.code import AppendUid, CopyUid
from pymap.interfaces.session import SessionInterface
from pymap.search import SearchParams, SearchCriteriaSet
from pymap.selected import SelectedMailbox

from .mailbox import KeyValMessage, KeyValMailbox
from .util import asyncenumerate

__all__ = ['KeyValSession']


class KeyValSession(SessionInterface[SelectedMailbox]):

    def __init__(self, inbox: KeyValMailbox, delimiter: str) -> None:
        super().__init__()
        self.inbox = inbox
        self.delimiter = delimiter

    @overload
    async def _load_updates(self, selected: SelectedMailbox,
                            mbx: KeyValMailbox) -> SelectedMailbox:
        ...

    @overload  # noqa
    async def _load_updates(self, selected: Optional[SelectedMailbox],
                            mbx: Optional[KeyValMailbox]) \
            -> Optional[SelectedMailbox]:
        ...

    async def _load_updates(self, selected, mbx):  # noqa
        if selected:
            if not mbx or selected.name != mbx.name:
                try:
                    mbx = await self.inbox.get_mailbox(selected.name)
                except MailboxNotFound:
                    selected.set_deleted()
                    return selected
            selected.set_uid_validity(mbx.uid_validity)
            async for uid, msg in mbx.items():
                selected.add_messages((uid, msg.permanent_flags))
        return selected

    @classmethod
    def _find_selected(cls, selected: Optional[SelectedMailbox],
                       mbx: KeyValMailbox) -> Optional[SelectedMailbox]:
        if selected and selected.name == mbx.name:
            return selected
        return mbx.selected_set.any_selected

    @classmethod
    async def _wait_for_updates(cls, mbx: KeyValMailbox,
                                wait_on: Event) -> None:
        try:
            or_event = wait_on.or_event(mbx.selected_set.updated)
            await or_event.wait(timeout=10.0)
        except TimeoutError:
            pass

    async def list_mailboxes(self, ref_name: str, filter_: str,
                             subscribed: bool = False,
                             selected: SelectedMailbox = None) \
            -> Tuple[Iterable[Tuple[str, Optional[str], Sequence[bytes]]],
                     Optional[SelectedMailbox]]:
        if filter_:
            list_tree = ListTree(self.delimiter).update('INBOX')
            if subscribed:
                list_tree.update(*await self.inbox.list_subscribed())
            else:
                list_tree.update(*await self.inbox.list_mailboxes())
            ret = [(entry.name, self.delimiter, entry.attrs)
                   for entry in list_tree.list_matching(ref_name, filter_)]
        else:
            ret = [("", self.delimiter, [b'Noselect'])]
        return ret, await self._load_updates(selected, None)

    async def get_mailbox(self, name: str, selected: SelectedMailbox = None) \
            -> Tuple[MailboxSnapshot, Optional[SelectedMailbox]]:
        mbx = await self.inbox.get_mailbox(name)
        snapshot = await mbx.snapshot()
        return snapshot, await self._load_updates(selected, mbx)

    async def create_mailbox(self, name: str,
                             selected: SelectedMailbox = None) \
            -> Optional[SelectedMailbox]:
        await self.inbox.add_mailbox(name)
        return await self._load_updates(selected, None)

    async def delete_mailbox(self, name: str,
                             selected: SelectedMailbox = None) \
            -> Optional[SelectedMailbox]:
        await self.inbox.remove_mailbox(name)
        return await self._load_updates(selected, None)

    async def rename_mailbox(self, before_name: str, after_name: str,
                             selected: SelectedMailbox = None) \
            -> Optional[SelectedMailbox]:
        await self.inbox.rename_mailbox(before_name, after_name)
        return await self._load_updates(selected, None)

    async def subscribe(self, name: str, selected: SelectedMailbox = None) \
            -> Optional[SelectedMailbox]:
        mbx = await self.inbox.get_mailbox('INBOX')
        await mbx.set_subscribed(name, True)
        return await self._load_updates(selected, mbx)

    async def unsubscribe(self, name: str, selected: SelectedMailbox = None) \
            -> Optional[SelectedMailbox]:
        mbx = await self.inbox.get_mailbox('INBOX')
        await mbx.set_subscribed(name, False)
        return await self._load_updates(selected, mbx)

    async def append_messages(self, name: str,
                              messages: Sequence[AppendMessage],
                              selected: SelectedMailbox = None) \
            -> Tuple[AppendUid, Optional[SelectedMailbox]]:
        mbx = await self.inbox.get_mailbox(name, try_create=True)
        if mbx.readonly:
            raise MailboxReadOnly(name)
        dest_selected = self._find_selected(selected, mbx)
        uids: List[int] = []
        for append_msg in messages:
            msg = mbx.parse_message(append_msg)
            msg = await mbx.add(msg, recent=not dest_selected)
            if dest_selected:
                dest_selected.session_flags.add_recent(msg.uid)
            uids.append(msg.uid)
        mbx.selected_set.updated.set()
        return (AppendUid(mbx.uid_validity, uids),
                await self._load_updates(selected, mbx))

    async def select_mailbox(self, name: str, readonly: bool = False) \
            -> Tuple[MailboxSnapshot, SelectedMailbox]:
        mbx = await self.inbox.get_mailbox(name)
        selected = SelectedMailbox(name, readonly or mbx.readonly,
                                   selected_set=mbx.selected_set)
        if not selected.readonly:
            recent_msgs: List[KeyValMessage] = []
            async for msg in mbx.messages():
                if msg.recent:
                    msg.recent = False
                    selected.session_flags.add_recent(msg.uid)
                    recent_msgs.append(msg)
            await mbx.save_flags(*recent_msgs)
        snapshot = await mbx.snapshot()
        return snapshot, await self._load_updates(selected, mbx)

    async def check_mailbox(self, selected: SelectedMailbox,
                            wait_on: Event = None,
                            housekeeping: bool = False) -> SelectedMailbox:
        mbx = await self.inbox.get_mailbox(selected.name)
        if housekeeping:
            await mbx.cleanup()
        if wait_on:
            await self._wait_for_updates(mbx, wait_on)
        return await self._load_updates(selected, mbx)

    async def fetch_messages(self, selected: SelectedMailbox,
                             sequence_set: SequenceSet,
                             attributes: FrozenSet[FetchAttribute]) \
            -> Tuple[Iterable[Tuple[int, KeyValMessage]], SelectedMailbox]:
        mbx = await self.inbox.get_mailbox(selected.name)
        ret = [(seq, msg) async for seq, msg
               in mbx.find(sequence_set, selected)]
        if not selected.readonly and any(attr.set_seen for attr in attributes):
            for _, msg in ret:
                msg.permanent_flags.add(Seen)
            await mbx.save_flags(msg for _, msg in ret)
        return ret, await self._load_updates(selected, mbx)

    async def search_mailbox(self, selected: SelectedMailbox,
                             keys: FrozenSet[SearchKey]) \
            -> Tuple[Iterable[Tuple[int, KeyValMessage]], SelectedMailbox]:
        mbx = await self.inbox.get_mailbox(selected.name)
        ret: List[Tuple[int, KeyValMessage]] = []
        snapshot = selected.snapshot
        params = SearchParams(selected, max_seq=snapshot.exists,
                              max_uid=snapshot.max_uid)
        search = SearchCriteriaSet(keys, params)
        async for seq, msg in asyncenumerate(mbx.messages(), 1):
            if search.matches(seq, msg):
                ret.append((seq, msg))
        return ret, await self._load_updates(selected, mbx)

    async def expunge_mailbox(self, selected: SelectedMailbox,
                              uid_set: SequenceSet = None) -> SelectedMailbox:
        if selected.readonly:
            raise MailboxReadOnly(selected.name)
        mbx = await self.inbox.get_mailbox(selected.name)
        if not uid_set:
            uid_set = SequenceSet.all(uid=True)
        expunge_uids: List[int] = []
        async for _, msg in mbx.find(uid_set, selected):
            if Deleted in msg.permanent_flags:
                expunge_uids.append(msg.uid)
        for uid in expunge_uids:
            await mbx.delete(uid)
        mbx.selected_set.updated.set()
        return await self._load_updates(selected, mbx)

    async def copy_messages(self, selected: SelectedMailbox,
                            sequence_set: SequenceSet,
                            mailbox: str) \
            -> Tuple[Optional[CopyUid], SelectedMailbox]:
        mbx = await self.inbox.get_mailbox(selected.name)
        dest = await self.inbox.get_mailbox(mailbox, try_create=True)
        if dest.readonly:
            raise MailboxReadOnly(mailbox)
        dest_selected = self._find_selected(selected, dest)
        uids: List[Tuple[int, int]] = []
        async for _, msg in mbx.find(sequence_set, selected):
            source_uid = msg.uid
            msg = await dest.add(msg, recent=not dest_selected)
            if dest_selected:
                dest_selected.session_flags.add_recent(msg.uid)
            uids.append((source_uid, msg.uid))
        dest.selected_set.updated.set()
        return (CopyUid(dest.uid_validity, uids),
                await self._load_updates(selected, mbx))

    async def update_flags(self, selected: SelectedMailbox,
                           sequence_set: SequenceSet,
                           flag_set: FrozenSet[Flag],
                           mode: FlagOp = FlagOp.REPLACE) \
            -> Tuple[Iterable[int], SelectedMailbox]:
        if selected.readonly:
            raise MailboxReadOnly(selected.name)
        mbx = await self.inbox.get_mailbox(selected.name)
        permanent_flags = flag_set & mbx.permanent_flags
        session_flags = flag_set & mbx.session_flags
        messages: List[KeyValMessage] = []
        async for _, msg in mbx.find(sequence_set, selected):
            msg.update_flags(permanent_flags, mode)
            selected.session_flags.update(msg.uid, session_flags, mode)
            messages.append(msg)
        await mbx.save_flags(*messages)
        uids = [msg.uid for msg in messages]
        mbx.selected_set.updated.set()
        return uids, await self._load_updates(selected, mbx)