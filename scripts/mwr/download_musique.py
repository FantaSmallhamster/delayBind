"""Download pinned MuSiQue files from a recorded public mirror; never execute remote code."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'data/raw/musique_v1.0'
PROVENANCE = DEST / 'provenance'


def checks(path, item, cross_hash):
    size = path.stat().st_size
    if size != item['size']:
        raise ValueError(f"Size mismatch for {path.name}: {size} != {item['size']}")
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1(f'blob {size}\0'.encode())
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            sha256.update(chunk)
            git_sha1.update(chunk)
    if git_sha1.hexdigest() != item['blobId']:
        raise ValueError(f'Git blob mismatch for {path.name}')
    if cross_hash and sha256.hexdigest() != cross_hash:
        raise ValueError(f'Independent mirror SHA-256 mismatch for {path.name}')
    return {'bytes': size, 'sha256': sha256.hexdigest(), 'git_blob_sha1': git_sha1.hexdigest(),
            'mirror_git_blob_verified': True, 'independent_mirror_sha256_verified': bool(cross_hash)}


def main():
    metadata = json.loads((PROVENANCE / 'voidful__MuSiQue.json').read_text())
    other = json.loads((PROVENANCE / 'bdsaglam__musique.json').read_text())
    cross = {r['rfilename']: r['lfs']['sha256'] for r in other['siblings'] if r.get('lfs')}
    files = [r for r in metadata['siblings'] if r['rfilename'].endswith(('.json', '.jsonl'))]
    files.sort(key=lambda r: ('_train' in r['rfilename'], '_test' in r['rfilename'], r['rfilename']))
    revision = metadata['sha']
    manifest = {'download_date_local': '2026-10-29',
        'official_repository': 'https://github.com/StonyBrookNLP/musique',
        'official_download': 'https://drive.google.com/file/d/1tGdADlNjWFaHLeZZGShh2IRcpO6Lv24h/view',
        'official_download_status': 'Connection timed out repeatedly; original archive not obtained',
        'transport_repository': 'https://huggingface.co/datasets/voidful/MuSiQue',
        'transport_revision': revision, 'cross_check_repository': 'https://huggingface.co/datasets/bdsaglam/musique',
        'cross_check_revision': other['sha'],
        'verification_scope': 'Git blob identity for every mirror file and independent mirror SHA-256 where available; not an official publisher checksum',
        'files': [], 'errors': []}

    def download(item):
        name = item['rfilename']
        assert Path(name).name == name
        url = f'https://huggingface.co/datasets/voidful/MuSiQue/resolve/{revision}/{name}?download=true'
        path = DEST / name
        if path.exists():
            return {'name': name, 'source_url': url, **checks(path, item, cross.get(name)), 'already_present': True}
        partial = path.with_suffix(path.suffix + '.part')
        for attempt in range(1, 4):
            try:
                print(f'START {name} {item["size"] / 1024**2:.1f} MiB attempt={attempt}', flush=True)
                req = Request(url, headers={'User-Agent': 'MuSiQue-dataset-download/1.0'})
                downloaded = 0
                last = time.monotonic()
                with urlopen(req, timeout=45) as response, partial.open('wb') as handle:
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if time.monotonic() - last > 25:
                            print(f'PROGRESS {name} {downloaded / 1024**2:.1f}/{item["size"] / 1024**2:.1f} MiB', flush=True)
                            last = time.monotonic()
                result = checks(partial, item, cross.get(name))
                partial.rename(path)
                print(f'VERIFIED {name} {result["sha256"]}', flush=True)
                return {'name': name, 'source_url': url, **result, 'downloaded_at': datetime.now(timezone.utc).isoformat()}
            except Exception as exc:
                print(f'RETRY {name}: {type(exc).__name__}: {exc}', flush=True)
                if attempt == 3:
                    raise
                time.sleep(2 * attempt)

    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(download, item): item for item in files}
        for future in as_completed(pending):
            try:
                manifest['files'].append(future.result())
            except Exception as exc:
                manifest['errors'].append({'file': pending[future]['rfilename'], 'error': f'{type(exc).__name__}: {exc}'})
            manifest['files'].sort(key=lambda row: row['name'])
            (DEST / 'download_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    if manifest['errors']:
        raise SystemExit('Some downloads failed; see download_manifest.json')
    (DEST / 'SHA256SUMS').write_text(''.join(f"{r['sha256']}  {r['name']}\n" for r in manifest['files']))
    print(f'COMPLETE {len(manifest["files"])} files, {sum(r["bytes"] for r in manifest["files"]):,} bytes', flush=True)


if __name__ == '__main__':
    main()
