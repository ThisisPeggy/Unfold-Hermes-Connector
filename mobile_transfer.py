"""Ephemeral LAN upload page for moving phone files into the Browser extension."""

import asyncio
import base64
import ipaddress
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiohttp import web

from .attachments import MAX_ATTACHMENT_BYTES, MAX_SESSION_ATTACHMENTS, MAX_SESSION_BYTES
from .attachments import validate_uploaded_attachment


DEFAULT_MOBILE_PORT = 8766
DEFAULT_TRANSFER_TTL_SECONDS = 5 * 60
MAX_MOBILE_IMAGES = 6
BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass
class MobileTransfer:
    transfer_id: str
    token: str
    owner: object
    expires_at: float
    count: int = 0
    image_count: int = 0
    total_bytes: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry_task: asyncio.Task | None = None


class MobileTransferService:
    """Runs a separate LAN listener only while an authenticated transfer exists."""

    def __init__(self, emit, *, port=None, bind_host="0.0.0.0", advertised_host=None, ttl_seconds=DEFAULT_TRANSFER_TTL_SECONDS):
        self._emit = emit
        self.port = int(port if port is not None else os.getenv("HERMES_BROWSER_MOBILE_PORT", DEFAULT_MOBILE_PORT))
        self.bind_host = bind_host
        self.advertised_host = advertised_host or os.getenv("HERMES_BROWSER_MOBILE_HOST", "").strip()
        self.ttl_seconds = max(30, min(15 * 60, int(ttl_seconds)))
        self._runner = None
        self._site = None
        self._transfers = {}
        self._lifecycle_lock = asyncio.Lock()

    async def create(self, owner):
        async with self._lifecycle_lock:
            await self._discard_owner_locked(owner)
            host = self.advertised_host or discover_lan_address()
            await self._ensure_started_locked()
            transfer_id = secrets.token_urlsafe(18)
            token = secrets.token_urlsafe(32)
            expires_at = time.time() + self.ttl_seconds
            transfer = MobileTransfer(transfer_id, token, owner, expires_at)
            self._transfers[transfer_id] = transfer
            transfer.expiry_task = asyncio.create_task(self._expire(transfer_id, self.ttl_seconds))
        bracketed_host = f"[{host}]" if ":" in host else host
        return {
            "transfer_id": transfer_id,
            "url": f"http://{bracketed_host}:{self.port}/mobile/{transfer_id}#token={token}",
            "expires_at": _iso_time(expires_at),
            "ttl_seconds": self.ttl_seconds,
            "limits": {
                "file_bytes": MAX_ATTACHMENT_BYTES,
                "total_bytes": MAX_SESSION_BYTES,
                "files": MAX_SESSION_ATTACHMENTS,
                "images": MAX_MOBILE_IMAGES,
            },
        }

    async def cancel(self, owner, transfer_id=""):
        async with self._lifecycle_lock:
            removed = self._remove_matching_locked(owner, transfer_id)
            await self._stop_if_empty_locked()
        return {"cancelled": removed > 0}

    async def discard_owner(self, owner):
        async with self._lifecycle_lock:
            await self._discard_owner_locked(owner)
            await self._stop_if_empty_locked()

    async def close(self):
        async with self._lifecycle_lock:
            for transfer in list(self._transfers.values()):
                _cancel_task(transfer.expiry_task)
            self._transfers.clear()
            await self._stop_locked()

    async def _ensure_started_locked(self):
        if self._runner:
            return
        app = web.Application(client_max_size=MAX_ATTACHMENT_BYTES + 1024 * 1024)
        app.router.add_get("/mobile/{transfer_id}", self._page)
        app.router.add_get("/mobile/{transfer_id}/status", self._status)
        app.router.add_post("/mobile/{transfer_id}/files", self._upload)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.bind_host, self.port)
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        if self.port == 0 and getattr(site, "_server", None):
            sockets = getattr(site._server, "sockets", [])
            if sockets:
                self.port = int(sockets[0].getsockname()[1])

    async def _stop_if_empty_locked(self):
        if not self._transfers:
            await self._stop_locked()

    async def _stop_locked(self):
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _discard_owner_locked(self, owner):
        self._remove_matching_locked(owner, "")

    def _remove_matching_locked(self, owner, transfer_id):
        selected = [
            key for key, transfer in self._transfers.items()
            if transfer.owner is owner and (not transfer_id or key == transfer_id)
        ]
        for key in selected:
            transfer = self._transfers.pop(key)
            _cancel_task(transfer.expiry_task)
        return len(selected)

    async def _expire(self, transfer_id, delay):
        try:
            await asyncio.sleep(delay)
            async with self._lifecycle_lock:
                transfer = self._transfers.pop(transfer_id, None)
                if not transfer:
                    return
                try:
                    await self._emit(transfer.owner, "mobile_transfer.expired", {"transfer_id": transfer_id})
                finally:
                    await self._stop_if_empty_locked()
        except asyncio.CancelledError:
            pass

    def _authorized_transfer(self, request):
        transfer = self._transfers.get(request.match_info.get("transfer_id", ""))
        if not transfer or transfer.expires_at <= time.time():
            raise web.HTTPGone(text="This transfer has expired.")
        supplied = request.headers.get("Authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {transfer.token}"):
            raise web.HTTPUnauthorized(text="Invalid transfer token.")
        return transfer

    async def _page(self, request):
        if request.match_info.get("transfer_id", "") not in self._transfers:
            raise web.HTTPGone(text="This transfer has expired.")
        nonce = secrets.token_urlsafe(18)
        response = web.Response(text=_mobile_page(nonce), content_type="text/html", charset="utf-8")
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        })
        return response

    async def _status(self, request):
        transfer = self._authorized_transfer(request)
        return web.json_response({
            "ok": True,
            "expires_at": _iso_time(transfer.expires_at),
            "count": transfer.count,
            "limits": {"file_bytes": MAX_ATTACHMENT_BYTES, "files": MAX_SESSION_ATTACHMENTS},
        }, headers={"Cache-Control": "no-store"})

    async def _upload(self, request):
        transfer = self._authorized_transfer(request)
        async with transfer.lock:
            if self._transfers.get(transfer.transfer_id) is not transfer or transfer.expires_at <= time.time():
                raise web.HTTPGone(text="This transfer has expired.")
            if transfer.count >= MAX_SESSION_ATTACHMENTS:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_SESSION_ATTACHMENTS, actual_size=transfer.count + 1)
            try:
                reader = await request.multipart()
                part = await reader.next()
            except (AssertionError, ValueError) as exc:
                raise web.HTTPBadRequest(text="Expected one multipart file.") from exc
            if part is None or part.name != "file" or not part.filename:
                raise web.HTTPBadRequest(text="Expected one multipart file.")
            chunks = bytearray()
            while True:
                chunk = await part.read_chunk(size=64 * 1024)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_ATTACHMENT_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_ATTACHMENT_BYTES, actual_size=len(chunks))
            try:
                name, mime_type = validate_uploaded_attachment(part.filename, part.headers.get("Content-Type", ""), bytes(chunks))
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            is_image = mime_type.startswith("image/")
            if is_image and transfer.image_count >= MAX_MOBILE_IMAGES:
                raise web.HTTPBadRequest(text="image-count-limit")
            if transfer.total_bytes + len(chunks) > MAX_SESSION_BYTES:
                raise web.HTTPBadRequest(text="attachment-total-size-limit")
            file_id = secrets.token_urlsafe(12)
            payload = {
                "transfer_id": transfer.transfer_id,
                "file_id": file_id,
                "name": name,
                "mime_type": mime_type,
                "size": len(chunks),
                "data_url": f"data:{mime_type};base64,{base64.b64encode(chunks).decode('ascii')}",
            }
            try:
                async with self._lifecycle_lock:
                    if self._transfers.get(transfer.transfer_id) is not transfer or transfer.expires_at <= time.time():
                        raise web.HTTPGone(text="This transfer has expired.")
                    await self._emit(transfer.owner, "mobile_transfer.file", payload)
                    transfer.count += 1
                    transfer.image_count += int(is_image)
                    transfer.total_bytes += len(chunks)
            except web.HTTPException:
                raise
            except Exception as exc:
                async with self._lifecycle_lock:
                    self._remove_matching_locked(transfer.owner, transfer.transfer_id)
                    await self._stop_if_empty_locked()
                raise web.HTTPGone(text="The computer disconnected.") from exc
            return web.json_response({
                "ok": True, "file_id": file_id, "name": name, "size": len(chunks), "count": transfer.count,
            }, headers={"Cache-Control": "no-store"})


def discover_lan_address():
    """Return a non-loopback address that a phone on the LAN can reach."""
    candidates = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        candidates.append((probe.getsockname()[0], 0))
    except OSError:
        pass
    finally:
        probe.close()
    try:
        candidates.extend((info[4][0], 1) for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return select_lan_address(candidates)


def select_lan_address(candidates):
    """Prefer a routed LAN address while rejecting proxy/TUN fake-IP ranges."""
    ranked = []
    seen = set()
    for candidate, source_rank in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version != 4
            or address in BENCHMARK_NETWORK
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            continue
        if address in ipaddress.ip_network("192.168.0.0/16"):
            private_rank = 0
        elif address in ipaddress.ip_network("172.16.0.0/12"):
            private_rank = 1
        elif address in ipaddress.ip_network("10.0.0.0/8"):
            private_rank = 2
        elif address.is_global:
            private_rank = 3
        else:
            continue
        ranked.append(((int(source_rank), private_rank), candidate))
    if ranked:
        return min(ranked)[1]
    raise RuntimeError("No reachable local network address was found.")


def _cancel_task(task):
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()


def _iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _mobile_page(nonce):
    nonce_attr = json.dumps(nonce)[1:-1]
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Send to Tale</title>
<style nonce="{nonce_attr}">
:root{{color-scheme:light dark;font:16px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f3;color:#242424}}*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;padding:20px}}main{{width:min(100%,440px);background:#fff;border:1px solid #dededb;border-radius:16px;padding:24px;box-shadow:0 8px 28px rgba(0,0,0,.08)}}h1{{font-size:22px;margin:0 0 6px}}p{{margin:0 0 18px;color:#686868}}.actions{{display:grid;gap:10px}}label{{min-height:48px;display:flex;align-items:center;justify-content:center;border-radius:10px;font-weight:650;cursor:pointer}}.primary{{background:#246b45;color:#fff}}.secondary{{border:1px solid #c9c9c5;background:#f7f7f5}}input{{position:absolute;inline-size:1px;block-size:1px;opacity:0}}#files{{list-style:none;margin:20px 0 0;padding:0;display:grid;gap:8px}}#files li{{padding:10px 12px;border:1px solid #e4e4e0;border-radius:9px;overflow-wrap:anywhere}}#status{{margin-top:18px;min-height:24px;font-size:14px}}.error{{color:#a12626}}@media(prefers-color-scheme:dark){{:root{{background:#181818;color:#f1f1ef}}main{{background:#222;border-color:#3b3b3b}}p{{color:#aaa}}.secondary{{background:#2b2b2b;border-color:#4b4b4b}}#files li{{border-color:#3b3b3b}}}}@media(prefers-reduced-motion:no-preference){{label:active{{transform:scale(.98)}}}}
label:focus-within{{outline:3px solid #246b45;outline-offset:3px}}
</style></head><body><main><h1 id="title">Send to Tale</h1><p id="hint">Choose photos or files. They will be added to your open message on the computer, but never sent automatically.</p><div class="actions"><label class="primary"><span id="photoLabel">Take or choose photos</span><input id="photos" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" capture="environment" multiple></label><label class="secondary"><span id="fileLabel">Choose files</span><input id="picker" type="file" multiple></label></div><ul id="files" aria-label="Selected files"></ul><div id="status" role="status" aria-live="polite">Connecting…</div></main>
<script nonce="{nonce_attr}">
(()=>{{const id=location.pathname.split('/').filter(Boolean).pop();const key='hermes-mobile-'+id;const hash=new URLSearchParams(location.hash.slice(1));const token=hash.get('token')||sessionStorage.getItem(key)||'';if(hash.get('token')){{sessionStorage.setItem(key,token);history.replaceState(null,'',location.pathname)}}const status=document.querySelector('#status');const list=document.querySelector('#files');const inputs=[document.querySelector('#photos'),document.querySelector('#picker')];const zh=navigator.language.toLowerCase().startsWith('zh');if(zh){{document.documentElement.lang='zh-CN';document.querySelector('#title').textContent='发送到 Tale';document.querySelector('#hint').textContent='选择照片或文件。它们只会添加到电脑上当前编辑的消息，不会自动发送。';document.querySelector('#photoLabel').textContent='拍照或选择照片';document.querySelector('#fileLabel').textContent='选择文件';list.setAttribute('aria-label','已选择文件')}}const copy=zh?{{connecting:'正在连接…',ready:'已连接，可以选择文件',sending:'正在发送',sent:'已发送',failed:'发送失败',expired:'传输已失效，请在电脑上重新生成二维码'}}:{{connecting:'Connecting…',ready:'Connected. Choose files to send.',sending:'Sending',sent:'Sent',failed:'Could not send',expired:'This transfer expired. Create a new QR code on the computer.'}};const auth={{Authorization:'Bearer '+token}};function setStatus(text,error=false){{status.textContent=text;status.classList.toggle('error',error)}}async function check(){{if(!token)throw new Error(copy.expired);const response=await fetch(location.pathname+'/status',{{headers:auth,cache:'no-store'}});if(!response.ok)throw new Error(copy.expired);setStatus(copy.ready)}}async function upload(file){{const item=document.createElement('li');item.textContent=file.name+' · '+copy.sending+' 0%';list.append(item);const form=new FormData();form.append('file',file,file.name);await new Promise((resolve,reject)=>{{const xhr=new XMLHttpRequest();xhr.open('POST',location.pathname+'/files');xhr.setRequestHeader('Authorization','Bearer '+token);xhr.upload.onprogress=(event)=>{{if(event.lengthComputable)item.textContent=file.name+' · '+copy.sending+' '+Math.round(event.loaded/event.total*100)+'%'}};xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve():reject(new Error(xhr.responseText||copy.failed));xhr.onerror=()=>reject(new Error(copy.failed));xhr.send(form)}});item.textContent=file.name+' · '+copy.sent}}async function selected(event){{for(const file of event.target.files||[]){{try{{await upload(file);setStatus(copy.ready)}}catch(error){{setStatus(error.message||copy.failed,true);break}}}}event.target.value=''}}inputs.forEach(input=>input.addEventListener('change',selected));setStatus(copy.connecting);check().catch(error=>{{setStatus(error.message||copy.expired,true);inputs.forEach(input=>input.disabled=true)}})}})();
</script></body></html>'''
