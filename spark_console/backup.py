"""Encrypted SQLite/config backup. The private recovery key stays off the server."""
from __future__ import annotations

import argparse
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b'SPARKBACKUP1\n'
CHUNK = 1024 * 1024
KEY_NAMES = ('cookie.key', 'session.key', 'pii.key')
CONFIG_NAMES = ('.env.console', 'compose.console.yml')
ALLOWED = {'data/spark.db', 'manifest.json'} | {'secrets/'+n for n in KEY_NAMES} | {'config/'+n for n in CONFIG_NAMES}


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=MAGIC)


def generate_keypair(private: Path, public: Path):
    if private.exists() or public.exists():
        raise FileExistsError('Recovery key already exists; refusing overwrite')
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    with private.open('xb') as out:
        os.chmod(private, 0o600)
        out.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with public.open('xb') as out:
        out.write(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def _encrypt(source: Path, output: Path, public: Path):
    key = serialization.load_pem_public_key(public.read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError('Expected RSA public key of at least 3072 bits')
    secret, nonce = os.urandom(32), os.urandom(12)
    header = json.dumps({'key': base64.b64encode(key.encrypt(secret, _oaep())).decode(), 'nonce': base64.b64encode(nonce).decode()}).encode()
    aad = MAGIC + struct.pack('!I', len(header)) + header
    encryptor = Cipher(algorithms.AES(secret), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    with source.open('rb') as src, output.open('xb') as dst:
        os.chmod(output, 0o600)
        dst.write(aad)
        while block := src.read(CHUNK):
            dst.write(encryptor.update(block))
        dst.write(encryptor.finalize()); dst.write(encryptor.tag)
        dst.flush(); os.fsync(dst.fileno())


def _decrypt(source: Path, output: Path, private: Path):
    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    with source.open('rb') as src, output.open('xb') as dst:
        os.chmod(output, 0o600)
        magic = src.read(len(MAGIC))
        size_raw = src.read(4)
        if magic != MAGIC or len(size_raw) != 4:
            raise ValueError('Invalid backup header')
        size = struct.unpack('!I', size_raw)[0]
        if size > 16384:
            raise ValueError('Invalid backup header length')
        raw = src.read(size); header = json.loads(raw)
        secret = key.decrypt(base64.b64decode(header['key'], validate=True), _oaep())
        nonce = base64.b64decode(header['nonce'], validate=True)
        start = src.tell(); src.seek(-16, os.SEEK_END); end = src.tell(); tag = src.read(16)
        if end < start:
            raise ValueError('Truncated backup')
        decryptor = Cipher(algorithms.AES(secret), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(magic + size_raw + raw)
        src.seek(start); remaining = end - start
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ValueError('Truncated backup')
            remaining -= len(block); dst.write(decryptor.update(block))
        dst.write(decryptor.finalize())  # Authenticate before any extraction.


def _validate_db(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        if db.execute('pragma quick_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
        if db.execute('pragma foreign_key_check').fetchone() is not None:
            raise ValueError('Database foreign key check failed')


def _digest(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup_database(source: Path, destination: Path):
    """Publish a consistent online snapshot, including committed WAL pages."""
    if destination.exists():
        raise FileExistsError('Backup destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=destination.parent) as temporary:
        snapshot = Path(temporary)/'snapshot.db'
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
        os.chmod(snapshot, 0o600); _validate_db(snapshot)
        snapshot.rename(destination)


def create_backup(data: Path, secrets: Path, config: Path, public: Path, output: Path, keep: int = 7) -> Path:
    if keep < 1:
        raise ValueError('Keep at least one backup')
    sources = {'secrets/'+n: secrets/n for n in KEY_NAMES}
    sources.update({'config/'+n: config/n for n in CONFIG_NAMES})
    for path in [data/'spark.db', public, *sources.values()]:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError('Required backup source missing or symlink: '+str(path))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    final = output/f'spark-{stamp}.sparkbak'
    with tempfile.TemporaryDirectory(prefix='.creating-', dir=output) as temporary:
        work = Path(temporary); snapshot = work/'spark.db'
        backup_database(data/'spark.db', snapshot)
        sources['data/spark.db'] = snapshot
        manifest = {'version': 1, 'created_at': stamp, 'files': {name: _digest(path) for name,path in sources.items()}}
        manifest_path = work/'manifest.json'; manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        sources['manifest.json'] = manifest_path
        archive = work/'backup.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name,path in sources.items():
                tar.add(path, arcname=name, recursive=False)
        encrypted = work/'encrypted'; _encrypt(archive, encrypted, public)
        encrypted.rename(final)
    # Prune only archives owned by this command, after successful publication.
    archives = sorted(p for p in output.glob('spark-*.sparkbak') if p.is_file() and not p.is_symlink())
    for old in archives[:-keep]:
        old.unlink()
    status = output/'status.json'
    status_tmp = output/'status.json.tmp'
    status_tmp.write_text(json.dumps({'last_success': datetime.now(timezone.utc).isoformat(), 'archive': final.name, 'bytes': final.stat().st_size}), encoding='utf-8')
    status_tmp.replace(status)
    return final


def restore_backup(archive: Path, private: Path, destination: Path):
    if destination.exists():
        raise FileExistsError('Restore destination must not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=destination.parent) as temporary:
        work = Path(temporary); plain = work/'authenticated.tar.gz'
        _decrypt(archive, plain, private)
        tree = work/'files'; tree.mkdir(mode=0o700)
        with tarfile.open(plain, 'r:gz') as tar:
            members = tar.getmembers()
            if {m.name for m in members} != ALLOWED or len(members) != len(ALLOWED):
                raise ValueError('Unexpected backup members')
            if any(not m.isfile() for m in members) or sum(m.size for m in members) > 2*1024**3:
                raise ValueError('Invalid backup member type or size')
            for member in members:
                target = tree/member.name; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    os.chmod(target, 0o600); shutil.copyfileobj(src, dst, CHUNK)
        manifest = json.loads((tree/'manifest.json').read_text())
        if manifest.get('version') != 1 or set(manifest['files']) != ALLOWED-{'manifest.json'}:
            raise ValueError('Invalid backup manifest')
        for name,digest in manifest['files'].items():
            if _digest(tree/name) != digest:
                raise ValueError('Backup checksum mismatch')
        _validate_db(tree/'data/spark.db')
        tree.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    key = sub.add_parser('keygen'); key.add_argument('--private', type=Path, required=True); key.add_argument('--public', type=Path, required=True)
    create = sub.add_parser('create')
    for name in ['data','secrets','config','public','output']:
        create.add_argument('--'+name, type=Path, required=True)
    create.add_argument('--keep', type=int, default=7)
    restore = sub.add_parser('restore')
    for name in ['archive','private','destination']:
        restore.add_argument('--'+name, type=Path, required=True)
    args = vars(parser.parse_args()); command = args.pop('command')
    result = {'keygen':generate_keypair, 'create':create_backup, 'restore':restore_backup}[command](**args)
    print(str(result) if result else 'OK')


if __name__ == '__main__':
    main()
