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
    subject = "Foxway Smart Doorbell v3.5.3 (Build 36) - Opschoning t/m 20:16:52, UI Polish & LAN/WAN Badges"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")
    guard_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/module_guard.py --verify")
    triage_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/triage.py | head -45")
    clips_status = run_cmd("curl -s 'http://127.0.0.1:8090/api/v1/clips?limit=100' | jq '{count: length, oldest: .[-1].timestamp, newest: .[0].timestamp}'")

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
    <h1>🚀 Foxway Smart Doorbell v3.5.3 (Build 36)</h1>
    <p>Opschoning Opnames t/m 20:16:52 • Vereenvoudigde Instellingen • Knop Hoogte Fix • LAN/WAN Badges</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>📋 Overzicht van Uitgevoerde Wijzigingen in v3.5.3</h3>
      <table>
        <tr><th>Taak</th><th>Status</th><th>Resultaat & Toelichting</th></tr>
        <tr>
          <td><b>1. Opschoning Opnames t/m 20:16:52</b></td>
          <td><span class="badge badge-success">VOLTOOID</span></td>
          <td>Alle 516 oude clip-records en 430 events t/m 20:16:52 zijn verwijderd uit SQLite <code>foxway.db</code> en 17.167 bestanden van schijf verwijderd. Database <code>VACUUM</code> gedraaid. De APK is intact bewaard. Alleen opnames ná 20:16:52 zijn bewaard gebleven.</td>
        </tr>
        <tr>
          <td><b>2. Instellingen UI Vereenvoudiging</b></td>
          <td><span class="badge badge-success">VOLTOOID</span></td>
          <td>De 3 losse logboek/diagnose knoppen zijn verwijderd uit het hoofdscherm van Settings. Het 30-dagen logboek opent uitsluitend via het blauwe <b>i-icoon</b> rechtsboven in de TopAppBar. De '📤 Rapporteer Fout / Status-Dump' knop is netjes geïntegreerd in de dialoog zelf.</td>
        </tr>
        <tr>
          <td><b>3. Knoptekst Hoogte & Padding Fix</b></td>
          <td><span class="badge badge-success">VOLTOOID</span></td>
          <td>Vaste 48dp hoogtes op knoppen vervangen door <code>Modifier.heightIn(min = 48.dp)</code> met <code>contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp)</code>, <code>fontSize = 14.sp</code>, <code>maxLines = 1</code> en gecentreerde uitlijning. Geen tekst valt meer weg.</td>
        </tr>
        <tr>
          <td><b>4. LAN / WAN Verbindingsbadges</b></td>
          <td><span class="badge badge-success">VOLTOOID</span></td>
          <td>Verbindingsbadge in <code>LiveScreen.kt</code> gewijzigd van '🟢 Lokaal' / '🔵 Online (5G)' naar een strakke, professionele badge: <b>LAN</b> (groen) en <b>WAN</b> (blauw).</td>
        </tr>
        <tr>
          <td><b>5. Release Build & Deployment</b></td>
          <td><span class="badge badge-success">VOLTOOID</span></td>
          <td>Android Release APK v3.5.3 (versionCode 36) succesvol gebouwd via Docker en geplaatst op <code>/clips/foxway-deurbel.apk</code>. Backend geüpdatet met v3.5.3 changelog.</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>📊 Huidige Opnames Status ná Opschoning</h3>
      <pre>{clips_status}</pre>
    </div>

    <div class="card">
      <h3>🔒 Foxway Module Guard Verificatie</h3>
      <pre>{guard_status}</pre>
    </div>

    <div class="card">
      <h3>🔍 Triage Samenvatting</h3>
      <pre>{triage_status}</pre>
    </div>

    <div class="card">
      <h3>📦 Release APK & Backend Versie Info</h3>
      <p><b>APK Bestand:</b> <code>{apk_info}</code></p>
      <p><b>Git Commit:</b> <code>{git_head}</code></p>
      <p><b>Backend JSON:</b></p>
      <pre>{version_status}</pre>
    </div>

  </div>
  <div class="footer">
    <p>Foxway Smart Doorbell System • Server foxway (192.168.2.1) • Gegenereerd op 2026-11-01</p>
  </div>
</div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Smart Doorbell <{smtp_user}>"
    msg["To"] = recipient

    msg.attach(MIMEText(html_content, "html"))

    print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.as_string())
    print("Email successfully sent!")

if __name__ == "__main__":
    send_report()
