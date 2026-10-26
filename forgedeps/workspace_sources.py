"""Single-current source workspaces owned by ForgeDeps, not by a consumer."""
from pathlib import Path
import json
import shutil
import tempfile

from . import deps_build as deps, deps_sources as sources
from .home import prepare_home
from .source_cache import fetch_download


def verify_source(dependency, *, home):
    root = Path(home)
    source = root / 'sources' / dependency.name
    receipt = root / 'state/sources' / (dependency.name + '.json')
    if source.is_symlink() or not source.is_dir() or receipt.is_symlink() or not receipt.is_file():
        raise ValueError(f'Missing or unverified shared source: {source}')
    previous = json.loads(receipt.read_text(encoding='utf-8'))
    if previous.get('schema') != 1 or previous.get('requested') != sources.requested(dependency):
        raise ValueError(f'Locked source selection differs from shared workspace: {source}')
    return source


def prepare_source(dependency, *, home=None, offline=False, with_identity=False):
    root = prepare_home(home)
    name = dependency.name
    if not name or name in ('.', '..') or any(c in name for c in '/\\:'):
        raise ValueError('Invalid dependency source name')
    source = root / 'sources' / name
    source.parent.mkdir(exist_ok=True)
    records = root / 'state/sources'
    records.mkdir(parents=True, exist_ok=True)
    receipt = records / (name + '.json')
    request = sources.requested(dependency)
    with sources.lock_output(source):
        if source.is_symlink() or receipt.is_symlink():
            raise ValueError(f'Refusing symbolic-link source workspace: {source}')
        previous = json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else None
        actual = sources.identity(source) if source.exists() else None
        if actual is not None:
            if previous and previous.get('requested') == request:
                return (source, actual['content']) if with_identity else source
            if not previous or previous.get('pristine') != actual:
                raise ValueError(f'{name}: locked source selection changed but the current source has '
                                 f'local or unrecorded contents; preserved: {source}. '
                                 'Move it aside or explicitly inject it as a source override.')
        with tempfile.TemporaryDirectory(prefix=name + '-source-', dir=root / 'tmp') as temporary:
            scratch = Path(temporary)
            candidate = scratch / 'source'
            cache = root / 'downloads' / name
            cache.mkdir(parents=True, exist_ok=True)
            if dependency.source_kind == 'git' or (not dependency.source_kind and dependency.kind == 'git'):
                deps.fetch_git(cache, candidate, dependency, offline)
            else:
                archive = fetch_download(dependency.url, dependency.sha256, cache,
                                         offline=offline)
                unpack = scratch / 'unpack'
                unpack.mkdir()
                extracted = deps.extract(archive, unpack, dependency.root)
                if list(unpack.iterdir()) != [extracted]:
                    raise ValueError('Source archive must contain exactly one top-level directory')
                extracted.rename(candidate)
            pristine = sources.identity(candidate)
            if actual is not None and sources.identity(source) != actual:
                raise ValueError(f'Source changed during preparation; preserved: {source}')
            backup = scratch / 'previous'
            if source.exists():
                source.rename(backup)
            try:
                candidate.rename(source)
                deps.atomic_json(receipt, {'schema': 1, 'requested': request, 'pristine': pristine})
            except BaseException:
                if source.exists():
                    shutil.rmtree(source)
                if backup.exists():
                    backup.rename(source)
                raise
        return (source, pristine['content']) if with_identity else source
