"""Local, dependency-free onboarding page shared by both server profiles."""

from __future__ import annotations

import html
from pathlib import Path


HOSTS = ("jp-4-9-0-pag.ludia.net", "jp-4-9-0-pap.ludia.net")


def setup_resource(base_dir: str, request_path: str, host_header: str):
    path = request_path.rstrip("/") or "/"
    guide_dir = Path(base_dir) / "assets" / "guide"
    image_map = {
        "/setup/hosts_manager_lite_main.png": guide_dir / "hosts_manager_lite_main.png",
        "/setup/hosts_manager_lite_add_item.png": guide_dir / "hosts_manager_lite_add_item.png",
    }
    image_path = image_map.get(request_path)
    if image_path and image_path.is_file():
        return image_path.read_bytes(), "image/png"
    if path != "/setup":
        return None
    host = (host_header or "").split(":", 1)[0].strip()
    safe_host = html.escape(host or "SERVER_IP")
    images = ""
    if all(candidate.is_file() for candidate in image_map.values()):
        images = """
        <div class="shots">
          <figure><img src="/setup/hosts_manager_lite_main.png" alt="Hosts Manager Lite main screen">
          <figcaption>1. Tap + to add a host. Tap ▶ after both hosts are saved.</figcaption></figure>
          <figure><img src="/setup/hosts_manager_lite_add_item.png" alt="Hosts Manager Lite add host dialog">
          <figcaption>2. Enter the Computer IP without a port, then enter the hostname.</figcaption></figure>
        </div>"""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Dino Server setup</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4faf6;color:#183128;font:16px/1.55 system-ui,Segoe UI,sans-serif}}
main{{max-width:900px;margin:auto;padding:28px 18px 60px}}h1{{font-size:34px;margin:0}}h2{{margin-top:30px}}
.hero,.card{{background:white;border:1px solid #d7e8dc;border-radius:18px;padding:22px;margin:16px 0;box-shadow:0 8px 28px #1f6a3b12}}
.ip{{font:700 26px ui-monospace,Consolas,monospace;color:#167443}}code{{word-break:break-all;background:#edf7f0;padding:3px 6px;border-radius:6px}}
button{{border:0;border-radius:10px;background:#208f52;color:white;padding:10px 14px;font-weight:700;cursor:pointer}}
.shots{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}figure{{margin:0}}img{{width:100%;height:auto;border:1px solid #d7e8dc;border-radius:12px}}
figcaption{{font-size:14px;color:#49685a;margin-top:7px}}.ok{{color:#167443;font-weight:700}}.note{{color:#49685a}}
@media(max-width:650px){{.shots{{grid-template-columns:1fr}}h1{{font-size:28px}}}}
</style></head><body><main>
<section class="hero"><h1>Dino Server setup</h1><p>Computer IP</p><div class="ip">{safe_host}</div>
<p><button onclick="navigator.clipboard.writeText('{safe_host}')">Copy IP</button></p>
<p id="live">Checking the local server…</p></section>
<section class="card"><h2>iPhone / iPad</h2><ol>
<li>In Dino Server select <b>iOS</b>, start it, and wait for Ready.</li>
<li>Open Settings → Wi-Fi → ⓘ → Configure DNS → Manual.</li>
<li>Remove existing DNS servers, add <code>{safe_host}</code>, and tap Save.</li>
<li>In Safari open <code>http://{HOSTS[0]}/status/2.0/</code>.</li>
<li>Before opening the game, allow it under Settings → Privacy &amp; Security → Local Network.</li>
</ol><p class="note">Use the same local network. Return DNS to Automatic when you stop Dino Server.</p></section>
<section class="card"><h2>Android / Emulator</h2><ol>
<li>Select <b>Android / Emulator</b>, start Dino Server, and wait for Ready.</li>
<li>Install Hosts Manager Lite from Google Play. Choose <b>VPN</b>, not Root, allow the VPN, and finish setup.</li>
<li>Add <code>{safe_host}</code> → <code>{HOSTS[0]}</code>.</li>
<li>Add <code>{safe_host}</code> → <code>{HOSTS[1]}</code>.</li>
<li>Enter only the IP: no <code>http://</code> and no port. Tap ▶ and keep the hosts VPN enabled.</li>
<li>Open <code>http://{HOSTS[0]}/status/2.0/</code> in the Android browser.</li>
</ol>{images}<p class="note">Hosts Manager Lite may conflict with another active VPN.</p></section>
<script>fetch('/status/2.0/').then(r=>r.json()).then(()=>{{live.textContent='✓ Local server is ready';live.className='ok'}})
.catch(()=>{{live.textContent='The local status test failed. Open Diagnostics in Dino Server.'}})</script>
</main></body></html>"""
    return page.encode("utf-8"), "text/html; charset=utf-8"
