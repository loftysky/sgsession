from datetime import datetime
import functools
import itertools
import re
import sys

from .utils import expect_datetime, parse_isotime


def asyncable(func):
    @functools.wraps(func)
    def _wrapped(self, *args, **kwargs):
        if kwargs.pop('async', False):
            return self.session._submit_concurrent(func, self, *args, **kwargs)
        else:
            return func(self, *args, **kwargs)
    return _wrapped


class Entity(dict):
    
    """A Shotgun entity.
    
    This behaves much like the :class:`dict` the Shotgun
    API normally returns does, but understands the links bettween entities in
    its associated session.
    
    """
    
    def __init__(self, type_, id_, session):

        dict.__init__(self, type=type_, id=id_)

        self.session = session
        self.backrefs = {}

        # Do we have confirmation that this entity exists and has not been
        # retired on the server? None -> we have not checked yet.
        self._exists = None
    
    @property
    def cache_key(self):
        type_ = dict.get(self, 'type')
        id_ = dict.get(self, 'id')
        if type_ and id_:
            return (type_, id_)
        elif type_:
            return ('Detached-%s' % type_, id(self))
        elif id_:
            return ('Unknown', id_)
        else:
            return ('Detached-Unknown', id(self))
    
    def minimize(self, keys=(), strict=False):
        ret = self.minimal
        for key in keys:
            if self.session.schema:
                key = self._resolve_key(key)
            try:
                ret[key] = dict.__getitem__(self, key)
            except KeyError:
                if strict:
                    raise
        return ret
    
    @property
    def minimal(self):
        """The minimal representation of this entity; a :class:`dict` with type and id."""
        return dict(type=self['type'], id=self['id'])
    
    @property
    def url(self):
        return '%s/detail/%s/%s' % (self.session.base_url, self['type'], self['id'])
    
    def is_same_entity(self, other):
        type_ = dict.get(self, 'type')
        id_ = dict.get(self, 'id')
        return type_ == other.get('type') and id_ == other.get('id')

    def as_dict(self):
        """Return the entity and all linked entities as pure :class:`dict`.
        
        The first reference to an entity will have all availible fields, and
        any subsequent ones will be the minimal representation. This is the
        ideal format for serialization and remerging into a session.
        
        """
        return self._as_dict(self, set())
    
    @classmethod
    def _as_dict(cls, obj, visited):

        if isinstance(obj, (tuple, list)):
            return [cls._as_dict(x, visited) for x in obj]
        if not isinstance(obj, dict):
            return obj

        if isinstance(obj, cls):
            if obj in visited:
                return obj.minimal
            visited.add(obj)

        ret = {}
        for k, v in sorted(obj.iteritems()):
            ret[k] = cls._as_dict(v, visited)

        return ret
    
    @property
    def name(self):
        return self.get('$shotgun:name') or self.get('name') or self.get('code') or self.get('content')
    
    def _repr_type(self):
        type_ = self.get('type')
        if type_ and type_.startswith('Custom'):
            schema = self.session.schema
            if schema:
                return schema.repr_entity(type_)
        return type_

    def __repr__(self):
        name = self.name
        return '<Entity %s:%s%s at 0x%x>' % (self._repr_type(), self.get('id'), ' %r' % name if name else '', id(self))
    
    def __hash__(self):
        type_ = dict.get(self, 'type')
        id_ = dict.get(self, 'id')
        if not (type_ and id_):
            raise TypeError('entity must have type and id to be hashable')
        return hash((type_, id_))
    
    def __reduce__(self):
        # Pickling results in all of the data being dumped.
        # The linked session will do the same.
        return self.__class__, (dict.get(self, 'type'), dict.get(self, 'id'), self.session)

    def pprint(self, backrefs=None, depth=0):
        """Print this entity, all links and optional backrefs."""
        print ''.join(self._pprint(backrefs, depth, set()))
    
    def pformat(self, backrefs=None, depth=0):
        """Stringify this entity, all links and optional backrefs."""
        return ''.join(self._pprint(backrefs, depth, set()))

    def _pprint(self, backrefs, depth, visited):
        yield '%s:%s at 0x%x; ' % (self._repr_type(), self.get('id'), id(self))
        
        # Did you know that bools are ints?
        if isinstance(backrefs, bool):
            backrefs = sys.maxint if backrefs else 0
        elif backrefs is not None and not isinstance(backrefs, int):
            backrefs = 0
        
        if id(self) in visited:
            yield '...\n'
            return
        visited.add(id(self))
        
        if len(self) <= 2:
            yield '{}\n'
            return
        
        yield '{\n'
        depth += 1
        for k, v in sorted(self.iteritems()):
            if k in ('id', 'type'):
                continue
            if isinstance(v, Entity):
                yield '%s%s = ' % ('\t' * depth, k)
                for sub in v._pprint(backrefs, depth, visited):
                    yield sub
            else:
                yield '%s%s = %r\n' % ('\t' * depth, k, v)
        
        if backrefs is not None:
            for (type_, field), entities in sorted(self.backrefs.iteritems()):
                # Using their wierd filter syntax here.
                yield '%s$FROM$%s.%s: ' % (
                    '\t' * depth,
                    type_,
                    field,
                )
                if backrefs > 0:
                    yield '[\n'
                    depth += 1
                    for x in entities:
                        yield '%s- ' % ('\t' * depth, )
                        for sub in x._pprint(backrefs - 1, depth, visited):
                            yield sub
                    depth -= 1
                    yield '\t' * depth + ']\n'
                else:
                    yield ', '.join(str(x) for x in sorted(x['id'] for x in entities)) + '\n'
        
        depth -= 1
        yield '\t' * depth + '}\n'
    
    @asyncable
    def exists(self, check=True, force=False):
        """Determine if this entity still exists (non-retired) on the server.

        :param bool check: Check against the server if we don't already know.
        :param bool force: Always recheck with the server, even if we already know.
        :returns bool: True/False if it is known to exist or not, and None if
            we do not know.

        See :meth:`.Session.filter_exists` for the bulk version.

        """

        # Incomplete entities don't exist.
        if self.get('id') is None or self.get('type') is None:
            return False

        # Defer the logic to the session, which will update _exists for us.
        self.session.filter_exists([self], check, force)

        return self._exists

    def _resolve_key(self, key):
        try:
            schema = self.session.schema
        except AttributeError:
            schema = None
        if schema:
            return schema.resolve_one_field(self['type'], key)
        else:
            return key

    def __contains__(self, key):
        try:
            self[key]
        except KeyError:
            return False
        else:
            return True
    
    def __getitem__(self, key):

        if not isinstance(key, basestring):
            raise KeyError(key)

        if key in ('id', 'type'): # Prevent a loop.
            return dict.__getitem__(self, key)

        key = self._resolve_key(key)

        try:
            src = self
            remote = key
            while True:
                m = re.match(r'^(\w+)\.([A-Z]\w+)\.(.+)$', remote)
                if not m:
                    break
                local, type_, remote = m.groups()
                src = dict.__getitem__(src, local)
                if not isinstance(src, dict):
                    # TODO: Should this be a TypeError?
                    raise KeyError('') # will get replaced in a moment...
                if dict.__getitem__(src, 'type') != type_:
                    # TODO: Should this be a ValueError?
                    raise KeyError('') # will get replaced in a moment...
            return dict.__getitem__(src, remote)
        except KeyError:
            raise KeyError(key)
    
    def __setitem__(self, key, value):
        key = self._resolve_key(key)

        # Try to assert these are datetime.
        if key in ('updated_at', 'created_at'):
            try:
                value = parse_isotime(value)
            except ValueError as e:
                log.exception('%s is not a timestamp' % key)

        dict.__setitem__(self, key, self.session.merge(value))
    
    def setdefault(self, key, value):
        key = self._resolve_key(key)
        return dict.setdefault(self, key, self.session.merge(value))
    
    def update(self, *args, **kwargs):
        for x in itertools.chain(args, [kwargs]):
            self._update(x)
    
    def _update(self, data, over=None, created_at=None, depth=0, memo=None):
        
        created_at = expect_datetime(created_at, 'given to Entity.update at depth {depth}', depth=depth)

        data = dict(data) # We will mutate it, so copy.

        # There is no need to resolve the schema at large, since __setitem__
        # will handle it for us.

        # Convert datetimes to UTC
        for k, v in data.iteritems():
            if isinstance(v, datetime) and v.tzinfo is not None:
                data[k] = datetime(*v.utctimetuple()[:6])
        
        # Pre-process deep linked names.
        for k, v in data.items():
            
            m = re.match(r'^(\w+)\.([A-Z]\w+)\.(.*)$', k)
            if m:
                field, type_, deep_field = m.groups()

                if v is None:

                    # None IDs lead to None entities.
                    if deep_field == 'id':
                        data[field] = None
                        continue
                    # None non-IDs are ignored iff the ID field also
                    # exists and is None.
                    else:
                        id_field = '%s.%s.id' % (field, type_)
                        if id_field in data and not data[id_field]:
                            continue

                if isinstance(data.setdefault(field, {}), dict):
                    # Ignore type mismatches and None fields.
                    if data[field].setdefault('type', type_) == type_ and v is not None:
                        data[field][deep_field] = v

                elif v is not None:
                    raise ValueError('Setting deep value on non-dict')
                # XXX: Is this dangerous?
                del data[k]
        
        # Determine if new values override old ones.
        if over:
            do_override = True
        elif over is None:
            if 'updated_at' in self and ('updated_at' in data or created_at):
                # Sometimes (due to an old bug in the sgcache), updated_at
                # and created_at would be strings. Even though we try to
                # coerce them all in __set__, sometimes they get through.
                self_updated_at = parse_isotime(self['updated_at'])
                data_updated_at = parse_isotime(data.get('updated_at', created_at))
                do_override = data_updated_at > self_updated_at
            else:
                do_override = True
        else:
            do_override = False
        
        for k, v in data.iteritems():
            
            # If it is an entity, then it will get automatically pulled into
            # place.
            v = self.session.merge(v, over, created_at, depth + 1, memo)
            
            if do_override or k not in self:
                
                self[k] = v

                # Establish a backref.
                if isinstance(v, Entity):
                    backrefs = v.backrefs.setdefault((self['type'], k), [])
                    if self not in backrefs:
                        backrefs.append(self)
    
    def copy(self):
        raise RuntimeError("cannot copy %s" % self.__class__.__name__)
    
    @asyncable
    def get(self, fields, default=None):
        """Get field value(s) if they exist, otherwise a default.
        
        :param fields: A ``str`` field name or collection of ``str`` field names.
        :param default: Default value to return when field does not exist.
        
        If passed a single field name as a ``str``, return the coresponding value.
        If passed field names as a list or tuple, return a tuple of coresponding values.
        
        """
        if isinstance(fields, (tuple, list)):
            res = []
            for f in fields:
                try:
                    res.append(self[f])
                except KeyError:
                    res.append(default)
            return tuple(res)

        try:
            return self[fields]
        except KeyError:
            return default
    
    @asyncable
    def fetch(self, fields, default=None, force=False):
        """Get field value(s), automatically fetching them from the server.
        
        :param fields: A ``str`` field name or collection of ``str`` field names.
        :param default: Default value to return when field does not exist.
        :param bool force: Force an update from the server, otherwise only query
            they server if fields have been requested that we do not already have.
        
        If passed a single field name as a ``str``, return the coresponding value.
        If passed field names as a list or tuple, return a tuple of coresponding values.
        
        See :meth:`.Session.fetch` for the bulk version.
        
        """
        is_single = not isinstance(fields, (tuple, list))
        if is_single:
            fields = [fields]
        self.session.fetch([self], fields, force=force)
        if is_single:
            return self.get(fields[0], default)
        else:
            return tuple(self.get(x, default) for x in fields)

    @asyncable
    def fetch_core(self):
        """Assert that all "important" fields exist on this Entity.
        
        See :meth:`.Session.fetch_core` for the bulk version.
        
        """
        self.session.fetch_core([self])
    
    @asyncable
    def fetch_heirarchy(self):
        """Fetch the full upward heirarchy (toward the Project) from the server.
        
        See :meth:`.Session.fetch_heirarchy` for the bulk version.
        
        """
        return self.session.fetch_heirarchy([self])
    
    @asyncable
    def fetch_backrefs(self, type_, field):
        """Fetch all backrefs to this Entity from the given type and field.
        
        See :meth:`.Session.fetch_backrefs` for the bulk version.
        
        """
        self.session.fetch_backrefs([self], type_, field)
    
    @asyncable
    def parent(self, fetch=True, extra=None):
        """Get the parent of this Entity, automatically fetching from the server."""
        try:
            field = self.session.parent_fields[self['type']]
        except KeyError:
            raise KeyError('%s does not have a parent type defined' % self['type'])
        
        # Fetch it if it exists (e.g. this isn't a Project) and we are allowed
        # to fetch.
        if field and fetch:
            fields = list(extra or [])
            fields.append(field)
            self.fetch(fields)
            self.setdefault(field, None)
        
        return self.get(field)
    
    @asyncable
    def project(self, fetch=True):
        """Get the project of this Entity, automatically fetching from the server.
        
        Depending on what part of the heirarchy is already loaded, many more
        entities will have their Project fetched by this single call.
        
        """
        
        # The most straightforward ways.
        if self['type'] == 'Project':
            return self
        try:
            return self['project']
        except KeyError:
            pass
                
        # Pass up the parental chain looking for a project.
        project = None
        parent = self.parent(fetch=False)
        if parent:
            if parent['type'] == 'Project':
                project = parent
            else:
                project = parent.project()
                
        # If we were given one from the parent, assume it.
        if project:
            self['project'] = project
            return project
                
        if fetch:
            # Fetch it ourselves; this should happen to the uppermost in a
            # heirachy that is not a Project.
            self.fetch(['project'])
            return self.setdefault('project', None)
    
