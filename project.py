#!/usr/bin/env python3
import os
import shutil
import os.path
import hashlib
import unicodedata
import subprocess
import fcntl
import time
import fnmatch

from datetime import datetime, timezone
from uuid import uuid4
from contextlib import contextmanager

from cli import Command
import rson

def UUID(): return str(uuid4())
def NOW(): return datetime.now(timezone.utc)
DOTVEX = '.vex'

MTIME_GRACE_SECONDS = 0.5

class VexError(Exception): pass

# Should not happen
class VexBug(VexError): pass
class VexCorrupt(VexError): pass

# Can happen
class VexLock(Exception): pass
class VexArgument(VexError): pass

# State Errors
class VexNoProject(VexError): pass
class VexNoHistory(VexError): pass
class VexUnclean(VexError): pass

class VexUnfinished(VexError): pass

class Codec:
    classes = {}
    def __init__(self):
        self.codec = rson.Codec(self.to_tag, self.from_tag)
    def register(self, cls):
        if cls.__name__ in self.classes:
            raise VexBug('Duplicate wire type')
        self.classes[cls.__name__] = cls
        return cls
    def to_tag(self, obj):
        name = obj.__class__.__name__
        if name not in self.classes: raise VexBug('An {} object cannot be turned into RSON'.format(name))
        return name, obj.__dict__
    def from_tag(self, tag, value):
        return self.classes[tag](**value)
    def dump(self, obj):
        return self.codec.dump(obj).encode('utf-8')
    def parse(self, buf):
        return self.codec.parse(buf.decode('utf-8'))

codec = Codec()

class objects:
    @codec.register
    class Branch:
        def __init__(self, uuid, head, base, init, upstream):
            self.uuid = uuid
            self.head = head
            self.base = base
            self.init = init
            self.upstream = upstream

    # Entries in commits
    @codec.register
    class Start:
        n = 0
        def __init__(self, timestamp, *, root, uuid, changelog):
            self.timestamp = timestamp
            self.uuid = uuid
            self.root = root
            self.changelog = changelog

        def next_n(self, kind):
            if kind == objects.Amend:
                return self.n
            return self.n+1

    @codec.register
    class Fork:
        n = 0
        def __init__(self, timestamp, *, base, uuid, changelog):
            self.base = base
            self.timestamp = timestamp
            self.uuid = uuid
            self.changelog = changelog

        def next_n(self, kind):
            if kind == objects.Amend:
                return self.n
            return self.n+1

    @codec.register
    class Stop:
        def __init__(self, n, timestamp, *, prev, uuid, changelog):
            self.n =n
            self.prev = prev
            self.timestamp = timestamp
            self.uuid = uuid
            self.changelog = changelog

    @codec.register
    class Restart:
        n = 0
        def __init__(self, timestamp, *, prev, base, changelog):
            self.prev = prev
            self.base = base
            self.timestamp = timestamp
            self.changelog = changelog

        def next_n(self, kind):
            if kind == objects.Amend:
                return self.n
            return self.n+1


    @codec.register
    class Prepare:
        def __init__(self, n, timestamp, *, prev, prepared, changes):
            self.n =n
            self.prev = prev
            self.prepared = prepared
            self.timestamp = timestamp
            self.changes = changes

        def next_n(self, kind):
            if kind == objects.Amend:
                raise VexBug('what')
            if kind in (objects.Commit, objects.Prepare):
                return self.n
            return self.n+1

    @codec.register
    class Commit:
        def __init__(self, n, timestamp, *, prev, prepared, root, changelog):
            self.n =n
            self.prev = prev
            self.prepared = prepared
            self.timestamp = timestamp
            self.root = root
            self.changelog = changelog

        def next_n(self, kind):
            if kind == objects.Amend:
                return self.n
            return self.n+1

    @codec.register
    class Amend:
        def __init__(self, n, timestamp, *, prev, root, changelog, prepared=None):
            self.n =n
            self.prev = prev
            self.timestamp = timestamp
            self.root = root
            self.changelog = changelog

        def next_n(self, kind):
            if kind == objects.Amend:
                return self.n
            return self.n+1

    @codec.register
    class Revert:
        def __init__(self, n, timestamp, *, prev, root, changelog):
            self.n =n
            self.prev = prev
            self.timestamp = timestamp
            self.root = root
            self.changelog = changelog

        def next_n(self, kind):
            return self.n+1


    @codec.register
    class Apply:
        def __init__(self, n, timestamp, *, prev, src, root, uuid, changelog):
            self.n =n
            self.prev = prev
            self.src=src
            self.timestamp = timestamp
            self.root = root
            self.uuid = uuid
            self.changelog = changelog

        def next_n(self, kind):
            return self.n+1

    # Apply
    # Replay (instead of Commit)

    @codec.register
    class Purge:
        pass

    @codec.register
    class Truncate:
        pass


    # Changelog and entries
    @codec.register
    class Changelog:
        def __init__(self, prev, summary, author, changes, message):
            self.prev = prev
            self.summary = summary
            self.author = author
            self.message = message
            self.changes = changes

    @codec.register
    class AddFile:
        def __init__(self, addr, properties):
            self.addr = addr
            self.properties = properties

    @codec.register
    class NewFile:
        def __init__(self, addr, properties):
            self.addr = addr
            self.properties = properties

    @codec.register
    class DeleteFile:
        def __init__(self):
            pass

    @codec.register
    class ChangeFile:
        def __init__(self, addr, properties):
            self.properties = properties
            self.addr = addr

    @codec.register
    class AddDir:
        def __init__(self, properties):
            self.properties = properties

    @codec.register
    class NewDir:
        def __init__(self, properties):
            self.properties = properties
    @codec.register
    class DeleteDir:
        def __init__(self):
            pass

    @codec.register
    class ChangeDir:
        def __init__(self, addr, properties):
            self.properties = properties
            self.addr = addr

    # Manfest Objects: Directories and entries
    @codec.register
    class Root:
        def __init__(self, entries, properties):
            self.entries = entries
            self.properties = properties

    @codec.register
    class Tree:
        def __init__(self, entries):
            self.entries = entries

    # Entries in a Tree Object
    # properties used to indicate 'large file' , 'large dir' options, ...
    # large dir is 'take this tree but treat it as a tree of prefixes...'
    # large file is 'addr points to  tree but it's list of @Chunks

    @codec.register
    class Dir:
        def __init__(self, addr,  properties):
            self.addr = addr
            self.properties = properties

    @codec.register
    class File:
        def __init__(self, addr, properties):
            self.addr = addr
            self.properties = properties

    @codec.register
    class Chunk:
        def __init__(self, addr):
            self.addr = addr

    # End of Repository State

    # History state

    @codec.register
    class Action:
        def __init__(self, time, command, changes=(), blobs=()):
            self.time = time
            self.command = command
            self.changes = changes
            self.blobs = blobs

    @codec.register
    class Switch:
        def __init__(self, time, command, prefix, session):
            self.time = time
            self.command = command
            self.prefix = prefix
            self.session = session


    @codec.register
    class Do:
        def __init__(self, prev, action):
            self.prev = prev
            self.action = action

    @codec.register
    class Redo:
        def __init__(self, next, dos):
            self.next = next
            self.dos = dos

    # Working copy state

    @codec.register
    class Session:
        def __init__(self,uuid, branch, prepare, commit, files,  mode=None, state=None):
            self.uuid = uuid
            self.branch = branch
            self.prepare = prepare
            self.commit = commit
            self.files = files
            self.mode = mode
            self.state = state

    @codec.register
    class Tracked:
        Kinds = set(('dir', 'file', 'ignore', 'stash'))
        States = set(('tracked', 'replaced', 'added', 'modified', 'deleted'))
        Unchanged = set(('tracked'))
        Changed = set(('added', 'modified', 'deleted', 'replaced'))

        def __init__(self, kind, state, *, working=False, addr=None, stash=None, mtime=None, properties=None, replace=None):
            if kind not in self.Kinds: raise VexBug('bad')
            if state not in self.States: raise VexBug('bad')
            self.kind = kind
            self.state = state
            self.working = working
            self.addr = addr
            self.mtime = mtime
            self.stash = stash
            self.properties = properties


# Stores

class DirStore:
    def __init__(self, dir):
        self.dir = dir

    def makedirs(self):
        os.makedirs(self.dir, exist_ok=True)
    def filename(self, name):
        return os.path.join(self.dir, name)
    def all(self):
        return os.listdir(self.dir())
    def exists(self, addr):
        return os.path.exists(self.filename(addr))
    def get(self, name):
        with open(self.filename(name), 'rb') as fh:
            return codec.parse(fh.read())
    def set(self, name, value):
        with open(self.filename(name),'w+b') as fh:
            fh.write(codec.dump(value))



class BlobStore:
    def __init__(self, dir):
        self.dir = dir

    def makedirs(self):
        os.makedirs(self.dir, exist_ok=True)

    def copy_from(self, other, addr):
        if other.exists(addr) and not self.exists(addr):
            src, dest = other.filename(addr), self.filename(addr)
            shutil.copyfile(src, dest)
        elif not self.exists(addr):
            raise VexCorrupt('Missing file {}'.format(other.filename(addr)))
    def move_from(self, other, addr):
        if other.exists(addr) and not self.exists(addr):
            src, dest = other.filename(addr), self.filename(addr)
            os.rename(src, dest)
        elif not self.exists(addr):
            raise VexCorrupt('Missing file {}'.format(other.filename(addr)))

    def make_copy(self, addr, filename):
        shutil.copyfile(self.filename(addr), filename)

    def addr_for_file(self, file):
        hash = hashlib.shake_256()
        with open(file,'rb') as fh:
            buf = fh.read(4096)
            while buf:
                hash.update(buf)
                buf = fh.read(4096)

        return "shake-256-20-{}".format(hash.hexdigest(20))

    def inside(self, file):
        return os.path.commonpath((file, self.dir)) == self.dir

    def addr_for_buf(self, buf):
        hash = hashlib.shake_256()
        hash.update(buf)
        return "shake-256-20-{}".format(hash.hexdigest(20))

    def filename(self, addr):
        return os.path.join(self.dir, addr)

    def exists(self, addr):
        return os.path.exists(self.filename(addr))

    def put_file(self, file, addr=None):
        addr = addr or self.addr_for_file(file)
        if not self.exists(addr):
            shutil.copyfile(file, self.filename(addr))
        return addr

    def put_buf(self, buf, addr=None):
        addr = addr or self.addr_for_buf(buf)
        if not self.exists(addr):
            with open(self.filename(addr), 'xb') as fh:
                fh.write(buf)
        return addr

    def put_obj(self, obj):
        buf = codec.dump(obj)
        return self.put_buf(buf)

    def get_file(self, addr):
        return self.filename(addr)

    def get_obj(self, addr):
        with open(self.filename(addr), 'rb') as fh:
            return codec.parse(fh.read())


# Action log: Used to track undo/redo and changes to repository state


class ActionLog:
    START = 'init'
    def __init__(self, dir):
        self.dir = dir
        self.store = BlobStore(os.path.join(dir,'entries'))
        self.redos = DirStore(os.path.join(dir,'redos' ))
        self.settings = DirStore(dir)

    def makedirs(self):
        os.makedirs(self.dir, exist_ok=True)
        self.store.makedirs()
        self.redos.makedirs()
        self.settings.set("current", self.START)
        self.settings.set("next", self.START)

    def empty(self):
        return self.current() == self.START

    def clean_state(self):
        if self.settings.exists("current") and self.settings.exists("next"):
            return self.settings.get("current") == self.settings.get("next")

    def current(self):
        return self.settings.get("current")

    def entries(self):
        current = self.current()
        out = []
        while current != self.START:
            obj = self.store.get_obj(current)
            redos = ()
            if self.redos.exists(current):
                redos = [self.store.get_obj(x).action.command for x in self.redos.get(current).dos]
            out.append((obj.action, redos))
            current = obj.prev

        return out


    @contextmanager
    def do(self, action):
        if not self.clean_state():
            raise VexCorrupt('Project history not in a clean state.')
        obj = objects.Do(self.current(), action)

        addr = self.store.put_obj(obj)
        self.settings.set("next", addr)

        yield action

        self.settings.set("current", addr)

    @contextmanager
    def undo(self):
        if not self.clean_state():
            raise VexCorrupt('Project history not in a clean state.')
        current = self.current()
        if current == self.START:
            yield None
            return

        obj = self.store.get_obj(current)

        if self.redos.exists(current):
            next = self.redos.get(current)
        else:
            next = None
        dos = [current]
        if obj.prev and self.redos.exists(obj.prev):
            dos.extend(self.redos.get(obj.prev).dos)
        redo = objects.Redo(next, dos)
        self.settings.set("next", obj.prev)

        yield obj.action

        new_next = self.redos.set(obj.prev, redo)
        self.settings.set("current", obj.prev)

    @contextmanager
    def redo(self, n=0):
        if not self.clean_state():
            raise VexCorrupt('Project history not in a clean state.')
        current = self.current()

        if not self.redos.exists(current):
            yield None
            return

        next = self.redos.get(current)

        do = next.dos.pop(n)

        obj = self.store.get_obj(do)
        redo = objects.Redo(next.next, next.dos)
        self.settings.set("next", do)

        yield obj.action

        self.redos.set(current, redo)
        self.settings.set("current", do)

    @contextmanager
    def rollback_new(self):
        if self.clean_state():
            yield
            return

        next = self.settings.get("next")
        obj = self.store.get_obj(next)
        old_current = obj.prev
        current = self.settings.get("current")
        if current != old_current:
            raise VexCorrupt('History is really corrupted: Interrupted transaction did not come after current change')
        yield obj.action
        self.settings.set("next", current)

    @contextmanager
    def restart_new(self):
        if self.clean_state():
            yield
            return
        next = self.settings.get("next")
        obj = self.store.get_obj(next)
        old_current = obj.prev
        current = self.settings.get("current")
        if current != old_current:
            raise VexCorrupt('History is really corrupted: Interrupted transaction did not come after current change')
        yield obj.action
        self.settings.set("current", next)


    def redo_choices(self):
        current = self.current()

        if not self.redos.exists(current):
            return

        next = self.redos.get(current)

        if next is None:
            return
        out = []
        for do in next.dos:
            obj = self.store.get_obj(do)
            out.append(obj.action)
        return out


class StatusChange:
    def __init__(self, project, command, active):
        self.project = project
        self.command = command
        self.now = NOW()
        self.old_sessions = {}
        self.new_sessions = {}
        self.active_uuid = active

    def get_change(self, uuid):
        return self.project.changes.get_obj(uuid)

    def get_branch(self, uuid):
        return self.project.changes.get_obj(uuid)

    def get_session(self, uuid):
        if uuid in self.new_sessions:
            return self.new_sessions[uuid]
        return self.project.sessions.get(uuid)

    def put_session(self, session):
        if session.uuid not in self.old_sessions:
            if self.project.sessions.exists(session.uuid):
                self.old_sessions[session.uuid] = self.project.sessions.get(session.uuid)
            else:
                self.old_sessions[session.uuid] = None
        self.new_sessions[session.uuid] = session

    def active(self):
        return self.get_session(self.active_uuid)

    def action(self):
        if self.new_sessions:
            changes = dict(sessions=dict(old=self.old_sessions, new=self.new_sessions))
            return objects.Action(self.now, self.command, changes, ())
        else:
            return objects.Action(self.now, self.command, {}, ())

    def prefix(self):
        return self.project.state.get("prefix")

    def repo_to_full_path(self, file):
        return self.project.repo_to_full_path(self.prefix(),file)

    def full_to_repo_path(self, file):
        return self.project.full_to_repo_path(self.prefix(),file)

    def refresh_active(self):
        copy = self.active()
        for name, entry in copy.files.items():
            if entry.working is None:
                continue
            path = self.repo_to_full_path(name)
            if entry.kind == "file":
                if not os.path.exists(path):
                    entry.state = "deleted"
                    entry.kind = entry.replace or entry.kind
                    entry.addr, entry.properties = None, None
                elif os.path.isdir(path):
                    if not entry.replace: entry.replace = entry.kind
                    entry.state = "replaced"
                    entry.kind = "dir"
                elif entry.state == 'added' or entry.state == 'replaced':
                    pass
                elif entry.state == 'deleted':
                    pass
                elif entry.state == 'tracked':
                    old_mtime = entry.mtime
                    if old_mtime is None:
                        new_addr = self.project.files.addr_for_file(path)
                        if new_addr != entry.addr:
                            entry.state = "modified"
                            entry.mtime = None
                        else:
                            new_mtime = os.path.getmtime(path)
                            if time.time() - new_mtime >= MTIME_GRACE_SECONDS:
                                entry.mtime = new_mtime
                    elif (old_mtime < os.path.getmtime(path)):
                        entry.state = "modified"
                        entry.mtime = None
                elif entry.state == 'modified':
                    pass
                else:
                    raise VexBug('welp')
            elif entry.kind == "dir":
                if not os.path.exists(path):
                    entry.kind = entry.replace or entry.kind
                    entry.state = "deleted"
                    entry.properties = None
                elif os.path.isfile(path):
                    entry.state = "replaced"
                    if not entry.replace: entry.replace = entry.kind
                    entry.kind = "file"
                    entry.properties = {}
                    entry.addr = None
                elif entry.state == 'added' or entry.state =='replaced':
                    pass
                elif entry.state == 'deleted':
                    pass
                elif entry.state == 'modified':
                    pass
                elif entry.state == 'tracked':
                    pass
                else:
                    raise VexBug('welp')
            elif entry.kind == "ignore":
                pass

        self.put_session(copy)
        return copy

    def active_changes(self, files=None):
        active = self.active()
        out = {}
        if not files:
            files_to_check = active.files.keys()
        else:
            # add prefixes to list
            files_to_check = set()
            for file in files:
                old = file
                while old != '/':
                    files_to_check.add(old)
                    old = os.path.split(old)[0]

        for repo_name in files_to_check:
            entry = active.files[repo_name]
            # and stashed items...? eh nm
            if entry.kind == 'file':
                if entry.state == "added":
                    filename = self.repo_to_full_path(repo_name)
                    addr = self.project.scratch.addr_for_file(filename)
                    out[repo_name]=objects.AddFile(addr, properties=entry.properties)
                elif entry.state == "replaced":
                    filename = self.repo_to_full_path(repo_name)
                    addr = self.project.scratch.addr_for_file(filename)
                    out[repo_name]=objects.NewFile(addr, properties=entry.properties)
                elif entry.state == "modified":
                    filename = self.repo_to_full_path(repo_name)
                    addr = self.project.scratch.addr_for_file(filename)
                    if addr != entry.addr:
                        out[repo_name]=objects.ChangeFile(addr, properties=entry.properties)
                elif entry.state == "deleted":
                    if entry.replace == "dir":
                        out[repo_name]=objects.DeleteDir()
                    else:
                        out[repo_name]=objects.DeleteFile()
                elif entry.state == 'tracked':
                    pass
                else:
                    raise VexBug('state {}'.format(entry.state))
            elif entry.kind =='dir':
                if entry.state == "added":
                    out[repo_name]=objects.AddDir(properties=entry.properties)
                elif entry.state == "replaced":
                    out[repo_name]=objects.NewDir(properties=entry.properties)
                elif entry.state == "modified":
                    out[repo_name]=objects.ChangeDir(properties=entry.properties)
                elif entry.state == "deleted":
                    if entry.replace == "file":
                        out[repo_name]=objects.DeleteFile()
                    else:
                        out[repo_name]=objects.DeleteDir()
                elif entry.state == 'tracked':
                    pass
                else:
                    raise VexBug('state {}'.format(entry.state))
            elif entry.kind == 'stash':
                if entry.state == "added":
                    addr = entry.stash
                    out[repo_name]=objects.AddFile(addr, properties=entry.properties)
                elif entry.state == "replaced":
                    addr = entry.stash
                    out[repo_name]=objects.NewFile(addr, properties=entry.properties)
                elif entry.state == "modified":
                    addr = entry.stash
                    if addr != entry.addr:
                        out[repo_name]=objects.ChangeFile(addr, properties=entry.properties)
                else:
                    raise VexBug('state {}'.format(entry.state))
            elif entry.kind == 'ignore':
                pass
            else:
                raise VexBug('kind')

        return out

class ProjectChange:
    refresh_active = StatusChange.refresh_active
    active_changes = StatusChange.active_changes

    def __init__(self, project, scratch, command):
        self.project = project
        self.scratch = scratch
        self.command = command
        self.now = NOW()
        self.old_branches = {}
        self.new_branches = {}
        self.old_names = {}
        self.new_names = {}
        self.old_sessions = {}
        self.new_sessions = {}
        self.old_settings = {}
        self.new_settings = {}
        self.new_changes = set()
        self.new_manifests = set()
        self.new_files = set()

    def active(self):
        return self.get_session(self.active_uuid)

    def prefix(self):
        return self.get_state("prefix")

    def repo_to_full_path(self, file):
        return self.project.repo_to_full_path(self.prefix(),file)

    def full_to_repo_path(self, file):
        return self.project.full_to_repo_path(self.prefix(),file)

    def active(self):
        session_uuid = self.get_state("active")
        return self.get_session(session_uuid)

    def update_active_files(self, files, remove):
        active = self.active()
        active.files.update(files)
        for r in remove:
            active.files.pop(r)
        self.put_session(active)

    def set_active_prepare(self, prepare_uuid):
        active = self.active()
        active.prepare = prepare_uuid
        self.put_session(active)

    def set_active_commit(self, commit_uuid):
        session = self.active()
        branch = self.get_branch(session.branch)
        if branch.head == session.commit: # descendent check
            branch.head = commit_uuid
            self.put_branch(branch)
        session.prepare = commit_uuid
        session.commit = commit_uuid
        self.put_session(session)

    def update_active_changes(self, changes):
        active = self.active()
        for name, change in changes.items():

            working = active.files[name].working

            if isinstance(change, objects.DeleteDir):
                active.files.pop(name)
            if isinstance(change, objects.DeleteFile):
                active.files.pop(name)
            else:
                if working:
                    path = self.repo_to_full_path(name)
                    mtime = os.path.getmtime(path)
                    if (time.time() - mtime) < MTIME_GRACE_SECONDS:
                        mtime = None
                else:
                    mtime = None

                if isinstance(change, objects.AddFile):
                    active.files[name] = objects.Tracked("file", "tracked", working=working, addr=change.addr, properties=change.properties, mtime=mtime)
                elif isinstance(change, objects.ChangeFile):
                    active.files[name] = objects.Tracked("file", "tracked", working=working, addr=change.addr, properties=change.properties, mtime=mtime)
                elif isinstance(change, objects.AddDir):
                    active.files[name] = objects.Tracked("dir", "tracked",  working=working, properties=change.properties, mtime=mtime)
                elif isinstance(change, objects.ChangeDir):
                    active.files[name] = objects.Tracked("dir", "tracked", working=working, properties=change.properties, mtime=mtime)
                else:
                    raise VexBug(change)


        self.put_session(active)

    def store_changed_files(self, changes):
        active = self.active()
        for name, change in changes.items():
            entry = active.files[name]
            if entry.kind == 'file' and entry.working:
                filename = self.repo_to_full_path(name)
                if os.path.isfile(filename) and isinstance(change, (objects.AddFile, objects.ChangeFile, objects.NewFile)):
                    addr = self.put_file(filename)
                    if addr != change.addr:
                        raise VexCorrupt('Sync')
            elif entry.kind == 'stash':
                self.new_files.add(entry.stash)


    def active_root(self, old, changes):
        active = self.active()
        dir_changes = {}
        for path, entry in sorted((p.split('/'),e) for p,e in changes.items()):
            prefix, name = "/".join(path[:-1]), path[-1]
            prefix = prefix or "/"
            name = name or "."
            if prefix not in dir_changes:
                dir_changes[prefix]  = {}
            dir_changes[prefix][name] = entry


        def apply_changes(prefix, addr, root=False):
            entries = {}
            changes = dir_changes.get(prefix, None)
            changed = bool(changes)
            properties = {}
            if addr:
                old = self.get_manifest(addr)
                if root:
                    properties = old.properties
                for name, entry in old.entries.items():
                    path = os.path.join(prefix, name)
                    if changes and name in changes:
                        change = changes.pop(name)
                        if isinstance(change, objects.NewFile):
                            entries[name] = objects.File(change.addr, change.properties)
                        elif isinstance(change, objects.NewDir):
                            new_addr = apply_changes(path, entry.addr)
                            entries[name] = objects.Dir(new_addr, change.properties)
                        elif isinstance(change, objects.DeleteFile):
                            if not isinstance(entry, objects.File): raise VexBug('cant delete a file not in repo')
                        elif isinstance(change, objects.DeleteDir):
                            if not isinstance(entry, objects.Dir): raise VexBug('cant delete a dir not in repo')
                            tree_addr = apply_changes(os.path.join(prefix, name), entry.addr)
                            if not tree_addr is None:
                                raise VexBug('non empty dir')
                        elif isinstance(change, objects.ChangeFile):
                            if not isinstance(entry, objects.File):
                                raise VexUnfinished(entry)
                            if entry.addr != change.addr:
                                entries[name] = objects.File(change.addr, change.properties)
                            else:
                                entries[name] = objects.File(entry.addr, change.properties)
                        elif isinstance(change, objects.ChangeDir):
                            if not isinstance(entry, objects.Dir):
                                raise VexUnfinished('nope')
                            new_addr = apply_changes(path, entry.addr)
                            entries[name] = objects.Dir(new_addr, change.properties)
                        elif isinstance(change, objects.AddDir):
                            tree_addr = apply_changes(os.path.join(prefix, name), None)
                            entries[name] = objects.Dir(tree_addr, change.properties)
                        elif isinstance(change, objects.AddFile):
                            entries[name] = objects.File(change.addr, change.properties)
                        else:
                            raise VexBug('nope')

                    elif isinstance(entry, objects.Dir):
                        path = os.path.join(prefix, name)
                        new_addr = apply_changes(path, entry.addr)
                        if new_addr != entry.addr:
                            changed = True
                            entries[name] = objects.Dir(new_addr, entry.properties)
                        else:
                            entries[name] = entry
                    elif isinstance(entry, objects.File):
                        entries[name] = entry
                    else:
                        raise VexBug('nope')

            if changes:
                for name, change in changes.items():
                    if name == ".":
                        if isinstance(change, objects.ChangeDir):
                            properties = change.properties
                        else:
                            raise VexBug('bad commit')

                    elif isinstance(change, (objects.AddDir, objects.NewDir)):
                        tree_addr = apply_changes(os.path.join(prefix, name), None)
                        entries[name] = objects.Dir(tree_addr, change.properties)
                    elif isinstance(change, (objects.AddFile, objects.NewFile)):
                        entries[name] = objects.File(change.addr, change.properties)
                    else:
                        raise VexBug('nope {}'.format(change))

            if not entries:
                return None
            elif changed:
                if root:
                    return self.put_manifest(objects.Root(entries, properties))
                else:
                    return self.put_manifest(objects.Tree(entries))
            else:
                return addr

        return apply_changes('/', old, root=True)

    def get_file(self, addr):
        if addr in self.new_files:
            return self.scratch.get_file(addr)
        return self.project.files.get_file(addr)

    def put_file(self, file):
        addr = self.scratch.put_file(file)
        self.new_files.add(addr)
        return addr

    def get_manifest(self, addr):
        if addr in self.new_manifests:
            return self.scratch.get_obj(addr)
        return self.project.manifests.get_obj(addr)

    def put_manifest(self, obj):
        addr = self.scratch.put_obj(obj)
        self.new_manifests.add(addr)
        return addr

    def get_change(self, addr):
        if addr in self.new_changes:
            return self.scratch.get_obj(addr)
        return self.project.changes.get_obj(addr)

    def put_change(self, obj):
        addr = self.scratch.put_obj(obj)
        self.new_changes.add(addr)
        return addr

    def get_session(self, uuid):
        if uuid in self.new_sessions:
            return self.new_sessions[uuid]
        return self.project.sessions.get(uuid)

    def put_session(self, session):
        if session.uuid not in self.old_sessions:
            if self.project.sessions.exists(session.uuid):
                self.old_sessions[session.uuid] = self.project.sessions.get(session.uuid)
            else:
                self.old_sessions[session.uuid] = None
        self.new_sessions[session.uuid] = session

    def get_branch(self, uuid):
        if uuid in self.new_branches:
            return self.new_branches[uuid]

        return self.project.branches.get(uuid)

    def put_branch(self, branch):
        if branch.uuid not in self.old_branches:
            if self.project.branches.exists(branch.uuid):
                self.old_branches[branch.uuid] = self.project.branches.get(branch.uuid)
            else:
                self.old_branches[branch.uuid] = None

        self.new_branches[branch.uuid] = branch

    def set_name(self, name, branch):
        if name not in self.old_names:
            if self.project.names.exists(name):
                self.old_names[name] = self.project.names.get(name)
            else:
                self.old_names[name] = None
        self.new_names[name] = branch.uuid

    def get_state(self, name):
        return self.project.state.get(name)

    def set_setting(self, name, value):
        if name not in self.old_settings:
            if self.project.settings.exists(name):
                self.old_settings[name] = self.project.settings.get(name)
            else:
                self.old_settings[name] = None
        self.new_settings[name] = value

    def get_setting(self, name):
        if name in self.new_settings:
            return self.new_settings[name]

        return self.project.setting.get(name)

    def action(self):
        if self.new_branches or self.new_names or self.new_sessions or self.new_state or self.new_changes or self.new_manfiests or self.new_files:
            branches = dict(old=self.old_branches, new=self.new_branches)
            names = dict(old=self.old_names, new=self.new_names)
            sessions = dict(old=self.old_sessions, new=self.new_sessions)
            settings = dict(old=self.old_settings, new=self.new_settings)

            blobs = dict(changes=self.new_changes, manifests=self.new_manifests, files=self.new_files)
            changes = dict(branches=branches,names=names, sessions=sessions, settings=settings)
            return objects.Action(self.now, self.command, changes, blobs)
        else:
            return objects.Action(self.now, self.command, {}, ())

class ProjectSwitch:
    def __init__(self, project, scratch, command):
        self.project = project
        self.scratch = scratch
        self.command = command
        self.prefix = {}
        self.session = {}
        self.now = NOW()

    def switch_prefix(self, new_prefix):
        self.prefix = {'old': self.project.prefix(), 'new': new_prefix}

    def switch_session(self, new_session):
        self.session = {'old': self.project.session(), 'new': new_session}

    def action(self):
        if self.prefix or self.session:
            return objects.Switch(self.now, self.command, self.prefix, self.session)
        else:
            return objects.Switch(self.now, self.command, {}, ())

class Project:
    def __init__(self, config_dir, working_dir):
        self.working_dir = working_dir
        self.dir = config_dir
        self.changes =   BlobStore(os.path.join(config_dir, 'project', 'commits'))
        self.manifests = BlobStore(os.path.join(config_dir, 'project', 'manifests'))
        self.files =     BlobStore(os.path.join(config_dir, 'project', 'files'))
        self.branches =   DirStore(os.path.join(config_dir, 'project', 'branches'))
        self.names =      DirStore(os.path.join(config_dir, 'project', 'branches', 'names'))
        self.state =      DirStore(os.path.join(config_dir, 'state'))
        self.sessions =   DirStore(os.path.join(config_dir, 'state', 'sessions'))
        self.actions =   ActionLog(os.path.join(config_dir, 'state', 'history'))
        self.scratch =   BlobStore(os.path.join(config_dir, 'scratch'))
        self.lockfile =            os.path.join(config_dir, 'lock')
        self.settings =  DirStore(os.path.join(config_dir, 'settings'))

    def makedirs(self):
        os.makedirs(self.dir, exist_ok=True)
        self.changes.makedirs()
        self.manifests.makedirs()
        self.files.makedirs()
        self.sessions.makedirs()
        self.branches.makedirs()
        self.names.makedirs()
        self.state.makedirs()
        self.actions.makedirs()
        self.scratch.makedirs()
        self.settings.makedirs()

    def makelock(self):
        with open(self.lockfile, 'xb') as fh:
            fh.write(b'# created by %d at %a\n'%(os.getpid(), str(NOW())))

    def exists(self):
        return os.path.exists(self.dir)

    def history_isempty(self):
        return self.actions.empty()

    def history(self):
        return self.actions.entries()

    def prefix(self):
        if self.state.exists("prefix"):
            return self.state.get("prefix")

    def active(self):
        if self.state.exists("active"):
            return self.sessions.get(self.state.get("active"))

    def clean_state(self):
        return self.actions.clean_state()

    def normalize(self, name):
        return unicodedata.normalize('NFC', name)

    def normalize_files(self, files):
        output = []
        for filename in files:
            filename = os.path.normpath(filename)
            if not filename.startswith(self.working_dir):
                raise VexError("{} is outside project".format(filename))
            output.append(filename)
        return files

    def repo_to_full_path(self, prefix, file):
        file = os.path.normpath(file)
        if os.path.commonpath((file, "/.vex")) == '/.vex':
            path = os.path.join(".vex/settings", os.path.relpath(file, '/.vex'))
        else:
            path = os.path.relpath(file, prefix)
        return os.path.normpath(os.path.join(self.working_dir, path))

    def full_to_repo_path(self, prefix, file):
        file = os.path.normpath(file)
        if os.path.commonpath((self.settings.dir, file)) == self.settings.dir:
            filename = os.path.relpath(file, self.settings.dir)
            return ("/.vex" if filename == "." else os.path.join("/.vex", filename))
        if file.startswith(self.dir):
            raise VexBug('nope. not .vex')
        # normalize name to NFC?
        filename = os.path.relpath(file, self.working_dir)
        filename = self.normalize(filename)
        if filename == '.':
            return prefix
        return os.path.join(prefix, filename)

    __locked = object()

    @contextmanager
    def lock(self, command):
        try:
            fh = open(self.lockfile, 'wb')
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, FileNotFoundError):
            raise VexLock('Cannot open project lockfile: {}'.format(self.lockfile))
        try:
                fh.truncate(0)
                fh.write(b'# locked by %d at %a\n'%(os.getpid(), str(NOW())))
                fh.write(codec.dump(command))
                fh.write(b'\n')
                fh.flush()
                yield self
                fh.write(b'# released by %d %a\n'%(os.getpid(), str(NOW())))
        finally:
                fh.close()


    def rollback_new_action(self):
        with self.actions.rollback_new() as action:
            if action:
                if isinstance(action, objects.Action):
                    self.apply_changes('old', action.changes)
                elif isinstance(action, objects.Switch):
                    # raise VexUnimplemented('this should probably pass but ...')
                    pass
                else:
                    raise VexBug('welp')
            return action

    def restart_new_action(self):
        with self.actions.restart_new() as action:
            if action:
                if isinstance(action, objects.Action):
                    self.copy_blobs(action.blobs)
                    self.apply_changes('new', action.changes)
                elif isinstance(action, objects.Switch):
                    pass
                else:
                    raise VexBug('welp')
            return action

    @contextmanager
    def do_nohistory(self, command):
        active = self.state.get("active")
        txn = StatusChange(self, command, active)
        yield txn
        action = txn.action()
        if action.changes:
            self.apply_changes('new', {'sessions': action.changes['sessions']})

    @contextmanager
    def do(self, command):
        if not self.actions.clean_state():
            raise VexCorrupt('Project history not in a clean state.')

        txn = ProjectChange(self, self.scratch, command)
        yield txn
        with self.actions.do(txn.action()) as action:
            if not action:
                return
            if isinstance(action, objects.Action):
                self.copy_blobs(action.blobs)
                self.apply_changes('new', action.changes)
            else:
                raise VexBug('action')

    @contextmanager
    def do_switch(self, command):
        if not self.actions.clean_state():
            raise VexCorrupt('Project history not in a clean state.')

        txn = ProjectSwitch(self, self.scratch, command)
        yield txn
        with self.actions.do(txn.action()) as action:
            if not action:
                return
            if isinstance(action, objects.Switch):
                self.apply_switch('new', action.prefix, action.session)
            else:
                raise VexBug('action')

    def undo(self):
        with self.actions.undo() as action:
            if not action:
                return
            if isinstance(action, objects.Action):
                self.apply_changes('old', action.changes)
            elif isinstance(action, objects.Switch):
                self.apply_switch('old', action.prefix, action.session)
            else:
                raise VexBug('action')

    def redo(self, choice):
        with self.actions.redo(choice) as action:
            if not action:
                return
            if isinstance(action, objects.Action):
                self.apply_changes('new', action.changes)
            elif isinstance(action, objects.Switch):
                self.apply_switch('new', action.prefix, action.session)
            else:
                raise VexBug('action')

    def redo_choices(self):
        return self.actions.redo_choices()


    # Take Action.changes and applies them to project
    def apply_changes(self, kind, changes):
        for key in changes:
            if key == 'branches':
                for name,value in changes['branches'][kind].items():
                    self.branches.set(name, value)
            elif key == 'names':
                for name,value in changes['names'][kind].items():
                    self.names.set(name, value)
            elif key == 'sessions':
                for name,value in changes['sessions'][kind].items():
                    self.sessions.set(name, value)
            elif key == 'settings':
                for name,value in changes['settings'][kind].items():
                    self.settings.set(name, value)
            else:
                raise VexBug('Project change has unknown values')

    # Takes Action.blobs and copies them out of the scratch directory
    def copy_blobs(self, blobs):
        for key in blobs:
            if key == 'changes':
                for addr in blobs['changes']:
                    self.changes.copy_from(self.scratch, addr)
            elif key == 'manifests':
                for addr in blobs['manifests']:
                    self.manifests.copy_from(self.scratch, addr)
            elif key =='files':
                for addr in blobs['files']:
                    self.files.copy_from(self.scratch, addr)
            else:
                raise VexBug('Project change has unknown values')

    def apply_switch(self, kind, prefix, session):
        if prefix:
            active_prefix = self.prefix()
            new_prefix = prefix[kind]
            if (kind =='new' and active_prefix != prefix['old']) or (kind =='old' and active_prefix != prefix['new']):
                raise VexCorruption('switch out of sync')
        else:
            active_prefix, new_prefix = self.prefix(), self.prefix()

        if session:
            active_session = self.state.get("active")
            new_session = session[kind]
            if (kind =='new' and active_session != session['old']) or (kind =='old' and active_session != session['new']):
                raise VexCorruption('switch out of sync')
        else:
            uuid = self.state.get('active')
            active_session, new_session = uuid, uuid

        active = self.sessions.get(active_session)
        active = self.stash_session(active)
        # check after stash, as it might be same
        new = self.sessions.get(new_session)
        self.clear_session(active_prefix, active)
        try:
            self.restore_session(new_prefix, new)
        except:
            self.restore_session(active_prefix, active)


    def stash_session(self, session):
        for name, entry in session.files.items():
            if name in ("/", "/.vex"): continue
            new_addr = None
            if entry.kind == "file":
                if entry.working is None:
                    continue
                path = self.repo_to_full_path(self.prefix(), name)
                if not os.path.exists(path):
                    entry.state = "deleted"
                    entry.addr, entry.properties = None, None
                elif os.path.isdir(path):
                    if not entry.replace: entry.replace = entry.kind
                    entry.state = "replaced"
                    entry.kind = "dir"
                elif entry.state == 'tracked':
                    new_addr = self.scratch.addr_for_file(path)
                    if new_addr != entry.addr:
                        entry.state = "modified"

            if entry.kind == 'file' and entry.state in ('added', 'replaced', 'modified'):
                entry.stash = self.scratch.put_file(path, new_addr)
                entry.kind = 'stash'

        self.sessions.set(session.uuid, session)
        return session

    def clear_session(self, prefix, session):
        if prefix != self.prefix() or session.uuid != self.state.get("active"):
            raise VexBug('no')
        dirs = set()
        for name, entry in session.files.items():
            if entry.kind == "ignore":
                continue
            if not entry.working:
                continue
            path = self.repo_to_full_path(prefix, name)
            if entry.kind == "file" or entry.kind == "stash":
                if os.path.commonpath((path, self.working_dir)) != self.working_dir:
                    raise VexBug('file outside of working dir inside tracked')
                if not os.path.isfile(path):
                    raise VexBug('sync')
                os.remove(path)
            elif entry.kind == "dir":
                if os.path.commonpath((path, self.working_dir)) != self.working_dir:
                    raise VexBug('file outside of working dir inside tracked')
                if name not in ("/", "/.vex", prefix):
                    dirs.add(path)
            elif entry.kind == "stash":
                pass
            else:
                raise VexBug('no')
            entry.working = None
            entry.mtime = None

        for dir in sorted(dirs, reverse=True, key=lambda x: x.split("/")):
            if dir in (self.working_dir, self.settings.dir):
                continue
            if not os.path.isdir(dir):
                raise VexBug('sync')
            if not os.listdir(dir):
                os.rmdir(dir)
        self.state.set('prefix', None)
        self.state.set('active', None)

    def restore_session(self, prefix, session):
        for name in sorted(session.files, key=lambda x:x.split('/')):
            entry = session.files[name]
            if os.path.commonpath((name, prefix)) != prefix and os.path.commonpath((name, '/.vex')) != '/.vex':
                entry.working = None
                entry.mtime = None
                continue
            else:
                entry.working = True
                entry.mtime = None
            if entry.kind == 'ignore':
                continue

            path = self.repo_to_full_path(prefix, name)

            if entry.kind =='dir':
                if name not in ('/', '/.vex', prefix):
                    os.makedirs(path, exist_ok=True)
            elif entry.kind =="file":
                self.files.make_copy(entry.addr, path)
            elif entry.kind == "stash":
                self.scratch.make_copy(entry.stash, path)
                entry.kind = "file"
                entry.stash = None
            elif entry.kind == "ignore":
                pass
            else:
                raise VexBug('kind')
        self.sessions.set(session.uuid, session)
        self.state.set('prefix', prefix)
        self.state.set('active', session.uuid)


    def switch(self, new_prefix):
        # check new prefix exists in repo
        if os.path.commonpath((new_prefix, "/.vex")) == "/.vex":
            raise VexArgument('bad arg')
        if new_prefix not in self.active().files and new_prefix != "/":
            raise VexArgument('bad prefix')
        with self.do_switch('switch') as txn:
            txn.switch_prefix(new_prefix)

    def log(self):
        with self.do_nohistory('log') as txn:
            session = txn.active()

        commit = session.prepare
        out = []
        while commit != session.commit:
            obj = self.changes.get_obj(commit)
            out.append('{}: *uncommitted* {}: {}'.format(obj.n, obj.__class__.__name__, commit))
            commit = getattr(obj, 'prev', None)
            # print \t date, file, message

        while commit != None:
            obj = self.changes.get_obj(commit)
            out.append('{}: committed {}: {}'.format(obj.n, obj.__class__.__name__, commit))
            commit = getattr(obj, 'prev', None)
        return out

    def status(self):
        with self.do_nohistory('status') as txn:
            return txn.refresh_active().files

    def list_dir(self, dir):
        output = []
        scan = [dir]
        while scan:
            dir = scan.pop()
            for f in os.listdir(dir):
                if not self.match_filename(f): continue
                f = os.path.join(dir, f)
                if os.path.isdir(f):
                    scan.append(f)
                    output.append(f)
                elif os.path.isfile(f):
                    output.append(f)
        return output

    def match_filename(self, name):
        ignore = self.settings.get('ignore')
        if ignore:
            if isinstance(ignore, str): ignore = ignore,
            for rule in ignore:
                if fnmatch.fnmatch(name, rule):
                    return False

        include = self.settings.get('include')
        if include:
            if isinstance(include, str): include = include,
            for rule in include:
                if fnmatch.fnmatch(name, rule):
                    return True

    def init(self, prefix, include, ignore):
        self.makedirs()
        if not self.history_isempty():
            raise VexNoHistory('cant reinit')
        if not prefix.startswith('/'):
            raise VexArgument('crap prefix')
        with self.do('init') as txn:
            author_uuid = UUID() 
            branch_uuid = UUID()

            project_uuid = None
            root_path = '/'
            if prefix != '/':
                root = objects.Root({os.path.relpath(prefix, root_path): objects.Dir(None, {}), '.vex': objects.Dir(None, {})}, {})
            else:
                root = objects.Root({'.vex': objects.Dir(None, {})}, {})
            root_uuid = txn.put_manifest(root)

            changes = {
                    '/' : objects.AddDir({}),
                    '/.vex' : objects.AddDir({}),
            }
            if prefix != '/':
                changes[prefix] = objects.AddDir({})
            changelog = objects.Changelog(prev=None, summary="init", message="", changes=changes, author=author_uuid)
            changelog_uuid = txn.put_manifest(changelog)

            commit = objects.Start(txn.now, uuid=branch_uuid, changelog=changelog_uuid, root=root_uuid)
            commit_uuid = txn.put_change(commit)

            branch_name = 'latest'
            branch = objects.Branch(branch_uuid, commit_uuid, None, commit_uuid, None)
            txn.put_branch(branch)
            txn.set_name(branch_name, branch)

            session_uuid = UUID()
            files = {
               '/': objects.Tracked('dir', 'tracked', working=None),
                '/.vex': objects.Tracked('dir', 'tracked', working=True),
                prefix: objects.Tracked('dir', 'tracked', working=True),
                '/.vex/ignore': objects.Tracked('file', 'added', working=True),
                '/.vex/include': objects.Tracked('file', 'added', working=True),
            }
            session = objects.Session(session_uuid, branch_uuid, commit_uuid, commit_uuid, files)
            txn.put_session(session)

            txn.set_setting("ignore", ignore)
            txn.set_setting("include", include)

        self.state.set("author", author_uuid)
        self.state.set("active", session_uuid)
        self.state.set("prefix", prefix)

    def diff(self, files):
        files = self.normalize_files(files) if files else None
        with self.do_nohistory('diff') as txn:
            session = txn.refresh_active()
            files = [txn.full_to_repo_path(filename) for filename in files] if files else None
            changes = txn.active_changes(files)

            output = {}
            for name in changes:
                e, c = session.files[name], changes[name]
                if e.kind == 'file':
                    output[name] = dict(old=self.files.filename(e.addr), new=self.repo_to_full_path(self.prefix(),name))
            output2 = {}
            for name, d in output.items():
                p = subprocess.run(["diff", d['old'], d['new']], stdout=subprocess.PIPE)
                output2[name] = p.stdout
            return output2

    def prepare(self, files):
        files = self.normalize_files(files) if files else None
        with self.do('prepare') as txn:
            session = txn.refresh_active()
            files = [txn.full_to_repo_path(filename) for filename in files] if files else None
            commit = session.prepare

            old_uuid = commit
            old = txn.get_change(commit)
            n = old.next_n('prepare')
            while old.n == n:
                old_uuid = old.prev
                old = txn.get_change(old.prev)

            changes = txn.active_changes(files)

            if not changes:
                return False

            if commit == old_uuid:
                commit = None

            txn.store_changed_files(changes)
            txn.update_active_changes(changes)

            prepare = objects.Prepare(n, txn.now, prev=old_uuid, prepared=commit, changes=changes)
            prepare_uuid = txn.put_change(prepare)

            txn.set_active_prepare(prepare_uuid)

    def commit(self, files, kind=objects.Commit):
        files = self.normalize_files(files) if files else None
        with self.do('commit') as txn:
            session = txn.refresh_active()
            files = [txn.full_to_repo_path(filename) for filename in files] if files else None

            changes = {}
            old_uuid = session.prepare
            old = txn.get_change(session.prepare)
            n = old.next_n(kind)
            while old.n == n:
                changes.update(old.changes)
                old_uuid = old.prev
                old = txn.get_change(old.prev)
                print()

            my_changes = txn.active_changes(files)

            changes.update(my_changes)

            if not changes:
                return False

            root_uuid = txn.active_root(old.root, changes)

            if root_uuid == old.root:
                return False

            txn.store_changed_files(my_changes)
            txn.update_active_changes(my_changes)

            author = txn.get_state('author')

            changelog = objects.Changelog(prev=old.changelog, summary="Summary", message="Message", changes=changes, author=author)
            changelog_uuid = txn.put_manifest(changelog)

            commit = kind(n=n, timestamp=txn.now, prev=old_uuid, prepared=session.prepare, root=root_uuid, changelog=changelog_uuid)
            commit_uuid = txn.put_change(commit)

            txn.set_active_commit(commit_uuid)


            return True

    def add(self, files):
        files = self.normalize_files(files)

        added = set()
        with self.do('add') as txn:
            session = txn.refresh_active()
            to_add = set()
            new_files = {}
            names = {}
            dirs = {}
            for filename in files:
                name = txn.full_to_repo_path(filename)
                if os.path.isfile(filename):
                    names[name] = filename
                elif os.path.isdir(filename):
                    dirs[name] = filename
                    to_add.add(filename)
                filename = os.path.split(filename)[0]
                name = os.path.split(name)[0]
                while name != '/' and name !='/.vex':
                    dirs[name] = filename
                    name = os.path.split(name)[0]
                    filename = os.path.split(filename)[0]


            for dir in to_add:
                for filename in self.list_dir(dir):
                    name = txn.full_to_repo_path(filename)
                    if os.path.isfile(filename):
                        names[name] = filename
                    elif os.path.isdir(filename):
                        dirs[name] = filename

            for name, filename in dirs.items():
                if name in session.files:
                    entry = session.files[name]
                    if entry.kind != 'dir':
                        replace = entry.replace
                        if replace == None and entry.kind != 'dir': replace = entry.kind
                        new_files[name] = objects.Tracked('dir', "replaced", working=True, properties={}, replace=replace)
                        added.add(filename)
                else:
                    new_files[name] = objects.Tracked('dir', "added", working=True, properties={})
                    added.add(filename)

            for name, filename in names.items():
                if name in session.files:
                    entry = session.files[name]
                    if entry.kind != 'file':
                        replace = entry.replace
                        if replace == None and entry.kind != 'file': replace = entry.kind
                        new_files[name] = objects.Tracked('file', "replaced", working=True, properties={}, replace=replace)
                        added.add(filename)
                else:
                    new_files[name] = objects.Tracked('file',"added", working=True, properties={})
                    added.add(filename)

            txn.update_active_files(new_files, ())
            return added

    def forget(self, files):
        files = self.normalize_files(files)

        with self.do('forget') as txn:
            session = txn.refresh_active()
            names = {}
            dirs = []
            changed = []
            for filename in files:
                name = txn.full_to_repo_path(filename)
                if name in session.files:
                    entry = session.files[name]
                    changed.append(filename)
                    if entry.working:
                        names[name] = entry
                        if entry.kind == 'dir':
                            p = "{}/".format(name)
                            for e in session.files:
                                if e.startswith(p):
                                    names[e] = session.files[e]
                                    changed.append(txn.repo_to_full_path(e))
            new_files = {}
            gone_files = set()

            for name, entry in names.items():
                if entry.state == 'added':
                    gone_files.add(name)
                else:
                    kind = entry.replace or entry.kind
                    new_files[name] = objects.Tracked(kind, "deleted", working=True, properties={})

            txn.update_active_files(new_files, gone_files)
            return changed

    def ignore(self, files):
        pass


def get_project(check=True, empty=True):
    working_dir = os.getcwd()
    while True:
        config_dir = os.path.join(working_dir, DOTVEX)
        if os.path.exists(config_dir):
            break
        new_working_dir = os.path.split(working_dir)[0]
        if new_working_dir == working_dir:
            raise VexNoProject('No vex project found in {}'.format(os.getcwd()))
        working_dir = new_working_dir
    p = Project(config_dir, working_dir)
    if check:
        if empty and p.history_isempty():
            raise VexNoHistory('Vex project exists, but `vex init` has not been run (or has been undone)')
        elif not p.clean_state():
            raise VexUnclean('Another change is already in progress. Try `vex debug:status`')
    return p

# CLI bits. Should handle environs, cwd, etc
vex_cmd = Command('vex', 'a database for files')
@vex_cmd.on_error()
def Error(path, args, exception, traceback):
    message = str(exception)
    if path:
        yield ("{}: {}".format(':'.join(path), message))
    else:
        yield ("vex: {}".format(message))

    if not isinstance(exception, VexError):
        yield ""
        yield ("Worse still, it's an error vex doesn't recognize yet. A python traceback follows:")
        yield ""
        yield (traceback)

    p = get_project(check=False)

    if p.exists() and not p.clean_state():
        p.rollback_new_action()

        if not p.clean_state():
            yield ('This is bad: The project history is corrupt, try `vex debug:status` for more information')
        else:
            yield ('Good news: The changes that were attempted have been undone')


vex_init = vex_cmd.subcommand('init')
@vex_init.run('--working --config --prefix --include... --ignore... [directory]')
def Init(directory, working, config, prefix, include, ignore):
    working_dir = working or directory or os.getcwd()
    config_dir = config or os.path.join(working_dir, DOTVEX)
    prefix = prefix or os.path.split(working_dir)[1] or 'root'
    prefix = os.path.join('/', prefix)

    include = include or ["*"] 
    ignore = ignore or [".*"]

    p = Project(config_dir, working_dir)

    if p.exists() and not p.clean_state():
        yield ('This vex project is unwell. Try `vex debug:status`')
    elif p.exists():
        if not p.history_isempty():
            raise VexError("A vex project already exists here")
        else:
            yield ('A empty project was round, re-creating project in "{}"...'.format(directory))
            with p.lock('init') as p:
                p.init(prefix, include, ignore)
    else:
        yield ('Creating vex project in "{}"...'.format(directory))
        p.init(prefix, include, ignore)
        p.makelock()

vex_add = vex_cmd.subcommand('add','add files to the project')
@vex_add.run('file...')
def Add(file):
    cwd = os.getcwd()
    if not file:
        files = [cwd]
    else:
        files = [os.path.join(cwd, f) for f in file]
    files = [os.path.normpath(f) for f in files]
    missing = [f for f in file if not os.path.exists(f)]
    if missing:
        raise VexArgument('cannot find {}'.format(",".join(missing)))
    p = get_project()
    with p.lock('add') as p:
        for f in p.add(files):
            f = os.path.relpath(f)
            yield "add: {}".format(f)

vex_forget = vex_cmd.subcommand('forget','remove files from the project, without deleting file')
@vex_forget.run('file...')
def Forget(file):
    if not file:
        return

    cwd = os.getcwd()
    files = [os.path.join(cwd, f) for f in file]
    files = [os.path.normpath(f) for f in files]
    missing = [f for f in file if not os.path.exists(f)]
    if missing:
        raise VexArgument('cannot find {}'.format(",".join(missing)))
    p = get_project()
    with p.lock('forget') as p:
        for f in p.forget(files):
            f = os.path.relpath(f)
            yield "forget: {}".format(f)

vex_diff = vex_cmd.subcommand('diff')
@vex_diff.run('[file...]')
def Diff(file):
    p = get_project()
    with p.lock('diff') as p:
        cwd = os.getcwd()
        files = [os.path.join(cwd, f) for f in file] if file else None
        for name, diff in  p.diff(files).items():
            yield name
            yield diff


vex_prepare = vex_cmd.subcommand('prepare', short="Save current working copy to prepare for commit", aliases=["save"])
@vex_prepare.run('[file...]')
def Prepare(file):
    p = get_project()
    yield ('Preparing')
    with p.lock('prepare') as p:
        cwd = os.getcwd()
        files = [os.path.join(cwd, f) for f in file] if file else None
        p.prepare(files)

vex_commit = vex_cmd.subcommand('commit')
@vex_commit.run('[file...]')
def Commit(file):
    p = get_project()
    yield ('Committing')
    with p.lock('commit') as p:
        cwd = os.getcwd()
        files = [os.path.join(cwd, f) for f in file] if file else None
        if p.commit(files):
            yield 'Committed'
        else:
            yield 'Nothing to commit'

        # check that session() and branch()

vex_log = vex_cmd.subcommand('log')
@vex_log.run()
def Log():
    p = get_project()
    for entry in p.log():
        yield (entry)
        yield ""

vex_history = vex_cmd.subcommand('history')
@vex_history.run()
def History():
    p = get_project()

    for entry,redos in p.history():
        alternative = ""
        if len(redos) == 1:
            alternative = "(can redo {})".format(redos[0])
        elif len(redos) > 0:
            alternative = "(can redo {}, or {})".format(",".join(redos[:-1]), redos[-1])

        yield "{}\t{}\t{}".format(entry.time, entry.command,alternative)
        yield ""

vex_status = vex_cmd.subcommand('status')
@vex_status.run()
def Status():
    p = get_project()
    cwd = os.getcwd()
    with p.lock('status') as p:
        files = p.status()
        for reponame in sorted(files, key=lambda p:p.split(':')):
            entry = files[reponame]
            if entry.working is None:
                yield "hidden:{:9}\t{} ".format(entry.state, reponame)
            elif reponame.startswith('/.vex/') or reponame == '/.vex':
                path = os.path.relpath(reponame, '/.vex')
                yield "{}:{:8}\t{}".format('setting', entry.state, path)
            else:
                path = os.path.relpath(reponame, p.prefix())
                yield "{:16}\t{} as {}".format(entry.state, path, reponame)

vex_switch = vex_cmd.subcommand('switch')
@vex_switch.run('prefix')
def switch(prefix):
    p = get_project()

    with p.lock('switch') as p:
        prefix = os.path.join(p.prefix(), prefix)
        p.switch(prefix)

vex_undo = vex_cmd.subcommand('undo')
@vex_undo.run()
def Undo():
    p = get_project()

    with p.lock('undo') as p:
        action = p.undo()
    if action:
        yield ('undid', action.command)

vex_redo = vex_cmd.subcommand('redo')
@vex_redo.run('--list? --choice')
def Redo(list, choice):
    p = get_project(empty=False)

    with p.lock('redo') as p:
        choices = p.redo_choices()

        if list:
            if choices:
                for n, choice in enumerate(choices):
                    yield (n, choice.time, choice.command)
            else:
                yield ('Nothing to redo')
        elif choices:
            choice = choice or 0
            action = p.redo(choice)
            if action:
                yield ('redid', action.command)
        else:
            yield ('Nothing to redo')

vex_debug = vex_cmd.subcommand('debug', 'run a command without capturing exceptions')
@vex_debug.run()
def Debug():
    yield ('Use vex debug <cmd> to run <cmd>, or use `vex debug:status`')

debug_status = vex_debug.subcommand('status')
@debug_status.run()
def DebugStatus():
    p = get_project(check=False)
    with p.lock('debug:status') as p:
        yield ("Clean history", p.clean_state())
        session = p.active()
        out = []
        if session:

            out.append("session: {}".format(session.uuid))
            out.append("at {}, started at {}".format(session.prepare, session.commit))

            branch = p.branches.get(session.branch)
            out.append("commiting to branch {}".format(branch.uuid))

            commit = p.changes.get_obj(session.prepare)
            out.append("last commit: {}".format(commit.__class__.__name__))
        else:
            if p.history_isempty():
                out.append("you undid the creation. try vex redo")
            else:
                out.append("no active session, but history, weird")
        out.append("")
        return "\n".join(out)


debug_restart = vex_debug.subcommand('restart')
@debug_restart.run()
def DebugRestart():
    p = get_project(check=False)
    with p.lock('debug:restart') as p:
        if p.clean_state():
            yield ('There is no change in progress to restart')
            return
        yield ('Restarting current action...')
        p.restart_new_action()
        if p.clean_state():
            yield ('Project has recovered')
        else:
            yield ('Oh dear')

debug_rollback = vex_debug.subcommand('rollback')
@debug_rollback.run()
def DebugRollback():
    p = get_project(check=False)
    with p.lock('debug:rollback') as p:
        if p.clean_state():
            yield ('There is no change in progress to rollback')
            return
        yield ('Rolling back current action...')
        p.rollback_new_action()
        if p.clean_state():
            yield ('Project has recovered')
        else:
            yield ('Oh dear')

vex_cmd.main(__name__)