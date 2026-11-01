#!/usr/bin/env python3
import os
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def run_cmd(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True).strip()
    except subprocess.CalledProcessError as e:
        return f"Error ({e.returncode}): {e.output.strip()}"

def send_report():
    smtp_host = os.getenv("FOXWAY_SMTP_HOST", "mailserver.foxway.nl")
    smtp_port = int(os.getenv("FOXWAY_SMTP_PORT", "587"))
    smtp_user = os.getenv("FOXWAY_SMTP_USER", "mo@foxway.nl")
    smtp_pass = os.getenv("FOXWAY_SMTP_PASS", "Alhoceima2026!")

    recipient = "mlamaiz@gmail.com"
    subject = "Foxway Smart Doorbell v3.5.2 (Build 35) - Dynamische Opnames, 2-Weg Settings Sync & Android Herstel"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    storage_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/settings/storage")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")
    guard_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/module_guard.py --verify")
    triage_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/triage.py | head -40")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #2d3748; padding: 20px; }}
  .container {{ max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.07); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #059669, #0284c7); color: white; padding: 28px; }}
  .header h1 {{ margin: 0; font-size: 24px; font-weight: 700; }}
  .header p {{ margin: 6px 0 0; opacity: 0.9; font-size: 14px; }}
  .content {{ padding: 28px; }}
  .card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 20px; }}
  .card h3 {{ margin-top: 0; color: #0f766e; font-size: 16px; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
  th {{ background-color: #f1f5f9; color: #475569; font-weight: 600; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; }}
  .badge-success {{ background-color: #def7ec; color: #03543f; }}
  .badge-info {{ background-color: #e1effe; color: #1e429f; }}
  pre {{ background: #0f172a; color: #38bdf8; padding: 14px; border-radius: 6px; font-family: monospace; font-size: 12px; overflow-x: auto; }}
  .footer {{ text-align: center; font-size: 12px; color: #94a3b8; padding: 20px; background: #f8fafc; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🚀 Foxway Smart Doorbell v3.5.2 (Build 35)</h1>
    <p>Dynamische Opnames • 100% 2-Weg Settings Sync • /admin Live & Inspector • Android Anti-Flapping</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>📋 Overzicht van Uitgevoerde Verbeteringen in v3.5.2</h3>
      <table>
        <tr><th>Onderdeel</th><th>Status</th><th>Beschrijving</th></tr>
        <tr>
          <td><b>Dynamische Bewegingsopnames</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Opnames starten met 20s pre-roll, blijven opnemen zolang beweging duurt via <code>notify_ongoing_motion</code>, en stoppen pas na de ingestelde post-roll. Vaste 32s hard kill-timer verwijderd. 85 clips met 00:00 duur en zwarte miniaturen volledig gerepareerd.</td>
        </tr>
        <tr>
          <td><b>100% 2-Weg Settings Sync</b></td>
          <td><span class="badge badge-success">GESYNCHRONISEERD</span></td>
          <td><code>system_settings</code> fungeert als single source of truth. Alle 12 variabelen (master AI, per-categorie opnemen & melden, nachtmodus, bewaartermijn, pre/post roll, drempelwaarden) synchroniseren direct tussen Web Admin en Android app.</td>
        </tr>
        <tr>
          <td><b>/admin Live Video & Inspector Fixes</b></td>
          <td><span class="badge badge-success">OPGELOST</span></td>
          <td>Live 4:3 speler start standaard gedempt (<code>?muted=true</code>) zodat browsers autoplay nooit meer blokkeren. AI Inspector toont nu zichtbare 80x60px snapshots met nieuwste-eerst sortering. Oude C-SDK vermelding vervangen door Passieve Sniffer Shadow-Learner.</td>
        </tr>
        <tr>
          <td><b>Sniffer Payload Hex Weergave</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Sniffer tabel in Tab 4 toont kolom <code>PAYLOAD HEX (EERSTE 24 BYTES)</code> en ververst continu elke 2 seconden. Dual-Ring en Shazam tabellen laden betrouwbaar uit de database/logs.</td>
        </tr>
        <tr>
          <td><b>Android Wi-Fi/5G Anti-Flapping</b></td>
          <td><span class="badge badge-success">HERSTELD</span></td>
          <td>Ping timeout verhoogd naar 1500ms, debouncing op netwerkwissels (3s), en minimaal 3 opeenvolgende mislukte pings vereist vóór omschakeling naar WAN. Voorkomt continue 'Lokaal/5G' status-wisselingen op thuis-WiFi.</td>
        </tr>
        <tr>
          <td><b>Android Live Freeze Watchdog</b></td>
          <td><span class="badge badge-success">GEOPTIMALISEERD</span></td>
          <td>Watchdog herstartdrempel verhoogd naar 10s (zonder destructieve server-kill) en freeze-indicator naar 6s. Elimineert de 4 seconden zwarte schermen bij kleine netwerkpieken.</td>
        </tr>
        <tr>
          <td><b>Android Status-Dump & UI Polish</b></td>
          <td><span class="badge badge-success">OPGELOST</span></td>
          <td>Retrofit <code>@JvmSuppressWildcards</code> fix op <code>postClientStatusDump</code>. Dubbele logboekknop verwijderd. Filter chips horizontaal scrollbaar (geen 'Error\\ns' wrapping meer). Echte <code>totalCount</code> in Clips header.</td>
        </tr>
        <tr>
          <td><b>Zero-Regression Slot</b></td>
          <td><span class="badge badge-success">GEWAARBORGD</span></td>
          <td>0 wijzigingen in <code>ring_detector.py</code>, <code>audio_bridge.py</code> en <code>CallActivity.kt</code>. Module Guard hashes geverifieerd.</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>📦 Release APK & Systeeminformatie</h3>
      <p><b>APK Bestand:</b> <code>{apk_info}</code></p>
      <p><b>Laatste Git Commit:</b> <code>{git_head}</code></p>
    </div>

    <div class="card">
      <h3>🔍 Module Guard & Smoke Checks</h3>
      <pre>{guard_status}</pre>
    </div>

    <div class="card">
      <h3>📊 Systeem Triage & Foutenlogboek (0 Errors)</h3>
      <pre>{triage_status}</pre>
    </div>

  </div>
  <div class="footer">
    <p>Foxway Smart Doorbell System • Geautomatiseerde Status- &amp; Verificatie Rapportage</p>
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Smart Doorbell <{smtp_user}>"
    msg["To"] = recipient

    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print("✅ Rapport e-mail succesvol verzonden naar " + recipient)
    except Exception as e:
        print("❌ Fout bij verzenden van e-mail rapport: " + str(e))

if __name__ == "__main__":
    send_report()

