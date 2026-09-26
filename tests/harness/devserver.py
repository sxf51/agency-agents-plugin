"""Serve a plugin page locally, wired to the plugin's real endpoints.

The dashboard runs a page in a sandboxed iframe and gives it a bridge object to
reach the backend. That is the only thing a page cannot have on its own, so a
page opened straight from disk does nothing at all.

This serves `pages/<name>/` over http and injects a development bridge with the
same method names as the host's, backed by `fetch` to this process instead of
`postMessage` to a dashboard. Every call lands on the plugin's own handler, so
clicking a button really runs the plugin's Python.

What it deliberately does not reproduce: the sandbox, the CSP, and the
authentication. A page that works here can still be blocked in the dashboard by
an off-origin asset - `checks.check_page_assets` is what catches that.

Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

API_PREFIX = "/__api/"
BRIDGE_PATH = "/__bridge.js"
DEV_USER = "dev"


def serve(
    *,
    plugin_dir: Path,
    page: str,
    host: Any,
    bundle: Any,
    port: int = 8800,
    locale: str = "en",
    dark: bool = False,
) -> str:
    """Start the server and return the URL to open. Blocks in `serve_forever`."""
    page_dir = plugin_dir / "pages" / page
    if not (page_dir / "index.html").is_file():
        msg = f"pages/{page}/index.html does not exist"
        raise FileNotFoundError(msg)

    context = {
        "pluginName": bundle.plugin.name,
        "pageName": page,
        "pageTitle": page,
        "locale": locale,
        "isDark": dark,
        "i18n": {},
    }
    handler = _make_handler(page_dir, host, bundle, context)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return url


def _make_handler(page_dir: Path, host: Any, bundle: Any, context: dict[str, Any]) -> type:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(page_dir), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:
            # One readable line per request instead of the default noise.
            print(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

        def do_GET(self) -> None:
            if self.path.split("?")[0] == BRIDGE_PATH:
                self._send(200, _bridge_script(context).encode("utf-8"), "application/javascript")
                return
            if self.path.startswith(API_PREFIX):
                self._dispatch("GET")
                return
            super().do_GET()

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def send_head(self) -> Any:
            # The entry document gets the bridge inserted, exactly as the host
            # does it, so the page needs no development-only markup.
            if self.path.split("?")[0] in ("/", "/index.html"):
                body = _inject_bridge((page_dir / "index.html").read_text(encoding="utf-8")).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return None
            return super().send_head()

        # -- plugin endpoints ------------------------------------------------

        def _dispatch(self, method: str) -> None:
            if not self.path.startswith(API_PREFIX):
                self._send(404, b'{"status":"error","message":"not_found"}', "application/json")
                return
            raw = self.path[len(API_PREFIX) :]
            endpoint, _, query = raw.partition("?")
            try:
                result = asyncio.run(
                    host.call_web(
                        bundle,
                        endpoint,
                        method,
                        username=DEV_USER,
                        query=_parse_query(query),
                        **self._read_payload(),
                    )
                )
            except Exception as exc:
                message = json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
                self._send(500, message.encode("utf-8"), "application/json")
                return
            self._send_result(result)

        def _read_payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            content_type = str(self.headers.get("Content-Type") or "")
            if content_type.startswith("application/json"):
                try:
                    return {"body": json.loads(raw)}
                except ValueError:
                    return {}
            # The page's upload() posts the bytes directly with the filename in
            # a header, which keeps this server free of a multipart parser.
            return {
                "upload": raw,
                "filename": self.headers.get("X-Upload-Filename") or "upload.bin",
                "content_type": self.headers.get("X-Upload-Type") or "application/octet-stream",
            }

        def _send_result(self, result: Any) -> None:
            if isinstance(result, dict | list):
                self._send(200, json.dumps(result).encode("utf-8"), "application/json")
                return
            status = getattr(result, "status_code", 200)
            media = getattr(result, "media_type", "application/json")
            content = getattr(result, "content", None)
            if content is not None:
                self._send(status, json.dumps(content).encode("utf-8"), "application/json")
                return
            path = getattr(result, "path", "")
            if path:
                data = Path(path).read_bytes()
                name = getattr(result, "filename", Path(path).name)
                self._send(status, data, media, {"Content-Disposition": f'attachment; filename="{name}"'})
                return
            generator = getattr(result, "generator", None)
            if generator is not None:
                self._stream(generator, media)
                return
            self._send(status, bytes(getattr(result, "body", b"")), media)

        def _stream(self, generator: Any, media: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            async def pump() -> None:
                async for chunk in generator:
                    self.wfile.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
                    self.wfile.flush()

            # A closed tab is the normal way a stream ends, and the socket error
            # it raises differs per platform - aborted on Windows, reset or a
            # broken pipe elsewhere. All of them are OSError.
            with contextlib.suppress(OSError):
                asyncio.run(pump())

        def _send(self, status: int, body: bytes, media: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _parse_query(raw: str) -> dict[str, str]:
    return dict(parse_qsl(raw))


def _inject_bridge(html: str) -> str:
    tag = f'<script src="{BRIDGE_PATH}"></script>'
    lowered = html.lower()
    head = lowered.find("<head>")
    if head >= 0:
        cut = head + len("<head>")
        return html[:cut] + "\n" + tag + html[cut:]
    return tag + "\n" + html


def _bridge_script(context: dict[str, Any]) -> str:
    """A development bridge with the same surface as the host's.

    Same method names and same return shapes, so page code written for the
    dashboard runs here unchanged.
    """
    return _BRIDGE_TEMPLATE.replace("__CONTEXT__", json.dumps(context))


_BRIDGE_TEMPLATE = """
/* Development bridge. Same API as the host's, backed by fetch to this server. */
;(function () {
  var context = __CONTEXT__
  var listeners = []
  var streams = {}

  document.documentElement.setAttribute('data-theme', context.isDark ? 'dark' : 'light')
  document.documentElement.setAttribute('lang', context.locale)

  function url(endpoint, params) {
    var query = ''
    if (params) {
      var parts = []
      Object.keys(params).forEach(function (key) {
        if (params[key] !== undefined && params[key] !== null) {
          parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(params[key]))
        }
      })
      if (parts.length) query = '?' + parts.join('&')
    }
    return '/__api/' + endpoint + query
  }

  function send(endpoint, method, params, body) {
    var init = { method: method, headers: {} }
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
    return fetch(url(endpoint, params), init).then(function (response) {
      var type = response.headers.get('Content-Type') || ''
      if (type.indexOf('application/json') === 0) {
        return response.json().then(function (payload) {
          if (!response.ok) throw new Error((payload && payload.message) || 'request failed')
          return payload
        })
      }
      if (!response.ok) throw new Error('HTTP ' + response.status)
      return response.blob()
    })
  }

  function asDataUrl(blob) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader()
      reader.onload = function () { resolve(String(reader.result || '')) }
      reader.onerror = function () { reject(new Error('could not read the response body')) }
      reader.readAsDataURL(blob)
    })
  }

  window.CapstonePluginPage = {
    ready: function () { return Promise.resolve(context) },
    getContext: function () { return context },
    getLocale: function () { return context.locale },
    getI18n: function () { return context.i18n || {} },
    t: function (key, fallback) { return fallback === undefined ? key : fallback },
    onContext: function (handler) {
      listeners.push(handler)
      handler(context)
      return function () { listeners.splice(listeners.indexOf(handler), 1) }
    },
    apiGet: function (endpoint, params) { return send(endpoint, 'GET', params) },
    apiPost: function (endpoint, body) { return send(endpoint, 'POST', null, body || {}) },
    apiPut: function (endpoint, body) { return send(endpoint, 'PUT', null, body || {}) },
    apiDelete: function (endpoint, params) { return send(endpoint, 'DELETE', params) },
    upload: function (endpoint, file) {
      return file.arrayBuffer().then(function (buffer) {
        return fetch(url(endpoint), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/octet-stream',
            'X-Upload-Filename': file.name,
            'X-Upload-Type': file.type || 'application/octet-stream',
          },
          body: buffer,
        }).then(function (response) {
          return response.json().then(function (payload) {
            if (!response.ok) throw new Error((payload && payload.message) || 'upload failed')
            return payload
          })
        })
      })
    },
    dataUrl: function (endpoint, params) {
      return fetch(url(endpoint, params)).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status)
        return response.blob().then(asDataUrl)
      })
    },
    download: function (endpoint, params, filename) {
      return fetch(url(endpoint, params)).then(function (response) {
        return response.blob().then(function (blob) {
          var href = URL.createObjectURL(blob)
          var anchor = document.createElement('a')
          anchor.href = href
          anchor.download = filename || 'download'
          document.body.appendChild(anchor)
          anchor.click()
          anchor.remove()
          URL.revokeObjectURL(href)
          return { saved: true }
        })
      })
    },
    subscribeSSE: function (endpoint, handlers) {
      var id = 'sse-' + Object.keys(streams).length
      var source = new EventSource(url(endpoint))
      streams[id] = source
      source.onopen = function () { if (handlers && handlers.onOpen) handlers.onOpen() }
      source.onmessage = function (event) {
        if (!handlers || !handlers.onMessage) return
        var parsed = null
        try { parsed = JSON.parse(event.data) } catch (error) { parsed = null }
        handlers.onMessage({ raw: event.data, parsed: parsed })
      }
      source.onerror = function () {
        source.close()
        delete streams[id]
        if (handlers && handlers.onClose) handlers.onClose()
      }
      return Promise.resolve(id)
    },
    unsubscribeSSE: function (id) {
      if (streams[id]) { streams[id].close(); delete streams[id] }
      return Promise.resolve({})
    },
    notify: function (message, tone) {
      /* The dashboard raises a toast; here it goes to the corner and the console. */
      console.log('[notify:' + (tone || 'info') + ']', message)
      var box = document.getElementById('__dev_toast')
      if (!box) {
        box = document.createElement('div')
        box.id = '__dev_toast'
        box.style.cssText =
          'position:fixed;right:16px;bottom:16px;z-index:9999;max-width:320px;padding:10px 14px;' +
          'border-radius:8px;font:13px sans-serif;color:#fff;background:#333;opacity:0;transition:opacity .2s'
        document.body.appendChild(box)
      }
      box.style.background = tone === 'error' ? '#c0392b' : tone === 'success' ? '#217a4b' : '#333'
      box.textContent = message
      box.style.opacity = '1'
      clearTimeout(box.__timer)
      box.__timer = setTimeout(function () { box.style.opacity = '0' }, 2600)
      return Promise.resolve({})
    },
  }

  /* Let the console flip theme and language, the way the dashboard would. */
  window.__setContext = function (patch) {
    context = Object.assign({}, context, patch)
    document.documentElement.setAttribute('data-theme', context.isDark ? 'dark' : 'light')
    listeners.slice().forEach(function (handler) { handler(context) })
  }
})()
"""
