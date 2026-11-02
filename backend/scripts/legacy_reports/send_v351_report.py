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
    subject = "Foxway Smart Doorbell v3.5.1 (Build 34) - Passieve Hex-Learner Sniffer, Arlo-UI & Module Guard"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    storage_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/settings/storage")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")
    guard_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/module_guard.py --verify")
    triage_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/triage.py | head -35")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #2d3748; padding: 20px; }}
  .container {{ max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.07); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #1e3a8a, #3b82f6); color: white; padding: 28px; }}
  .header h1 {{ margin: 0; font-size: 24px; font-weight: 700; }}
  .header p {{ margin: 6px 0 0; opacity: 0.9; font-size: 14px; }}
  .content {{ padding: 28px; }}
  .card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 20px; }}
  .card h3 {{ margin-top: 0; color: #1e3a8a; font-size: 16px; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
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
    <h1>🚀 Foxway Smart Doorbell v3.5.1 (Build 34)</h1>
    <p>Passieve Hex-Learner Sniffer • Arlo-Feed UI • Stuck Sweeper • Live Audio Fix • Module Guard</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>📋 Overzicht van Uitgevoerde Verbeteringen in v3.5.1</h3>
      <table>
        <tr><th>Onderdeel</th><th>Status</th><th>Beschrijving</th></tr>
        <tr>
          <td><b>Sniffer Bel-Trigger Ontkoppeld</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Sniffer laat telefoon NOOIT meer afgaan. Alleen Shazam Audio-Chime (en testknop) triggert oproep.</td>
        </tr>
        <tr>
          <td><b>Passieve Differential Byte-Learner</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Rollende 30s buffer slaat eerste 64 bytes op. Bij Shazam Chime worden unieke patronen vergeleken met achtergrondruis en gelogd in sniffer_learning.jsonl.</td>
        </tr>
        <tr>
          <td><b>Mismatch als WARNING</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Als Shazam afgaat en Sniffer ziet binnen 3.5s niets, wordt een [RING MISMATCH WARNING] gelogd (geen error).</td>
        </tr>
        <tr>
          <td><b>Stuck-Recording Sweeper</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Controleert elke 15s en bij /clips op opnames die >40s hangen. Garandeert READY status voor alle bestanden >10KB.</td>
        </tr>
        <tr>
          <td><b>Thumbnail Fallback</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>/api/v1/clips/{{id}}/thumbnail retourneert ALTIJD HTTP 200 (zoekt in 4 locaties of genereert 1x1 black JPEG, nooit 404).</td>
        </tr>
        <tr>
          <td><b>Arlo-Feed UI (Android)</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>96dp timeline rij met 136dp 4:3 miniatuur, play-icoon, categorie, datum/tijd, bestandsgrootte en 3 compacte knoppen (Download, Ster, Verwijderen).</td>
        </tr>
        <tr>
          <td><b>Live Audio Speaker Switch</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>MODE_NORMAL audio route met luidspreker inschakeling; go2rtc met aac+opus audio stream transcodering.</td>
        </tr>
        <tr>
          <td><b>Module Guard & Pre-Commit Hook</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Vergrendelt kernmodules (ai_detector, ring_detector, audio_bridge, WebSocketForegroundService, CallActivity) met SHA256 hashes + 5 smoke checks.</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>📱 Android APK & Build Status</h3>
      <p><b>APK Pad:</b> <code>/mnt/opslag/deurbel/clips/foxway-deurbel.apk</code></p>
      <p><b>APK Details:</b> {apk_info}</p>
      <p><b>Versie Info:</b></p>
      <pre>{version_status}</pre>
    </div>

    <div class="card">
      <h3>🔐 Module Guard & Smoke Checks</h3>
      <pre>{guard_status}</pre>
    </div>

    <div class="card">
      <h3>🔍 Triage Status Log</h3>
      <pre>{triage_status}</pre>
    </div>

    <div class="card">
      <h3>💾 Git Commit</h3>
      <pre>{git_head}</pre>
    </div>

  </div>
  <div class="footer">
    Foxway Smart Doorbell System • Live Server foxway (192.168.2.1)
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Smart Doorbell <{smtp_user}>"
    msg["To"] = recipient

    part = MIMEText(html_content, "html")
    msg.attach(part)

    print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.as_string())

    print(f"✅ Report successfully sent to {recipient}")

if __name__ == "__main__":
    send_report()

