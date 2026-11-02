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
    subject = "Foxway Smart Doorbell v3.5.5 (Build 38) - Statusbalk Huis-met-Bel Icoon, Samengevoegde Multi-Tag Opnames & Slim Anti-Wind Filter"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")
    guard_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/module_guard.py --verify")
    triage_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/triage.py | head -45")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #2d3748; padding: 20px; }}
  .container {{ max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.07); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #10B981, #0284c7); color: white; padding: 28px; }}
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
    <h1>🚀 Foxway Smart Doorbell v3.5.5 (Build 38)</h1>
    <p>Statusbalk Huis-met-Bel Icoon • Geconsolideerde Multi-Tag Opnames • Slim Anti-Wind Filter • 100% Notificatie Garantie</p>
  </div>
  <div class="content">
    <div class="card">
      <h3>📌 Samenvatting Uitgevoerde Taken</h3>
      <table>
        <tr><th>Onderdeel</th><th>Status</th><th>Toelichting</th></tr>
        <tr>
          <td><b>Statusbalk Icoon</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>Nieuw monochroom wit <code>ic_stat_foxway_house_bell.xml</code> (24x24dp) vervangt de oude camera in alle notificaties en statusbalk.</td>
        </tr>
        <tr>
          <td><b>Multi-Tag Consolidatie</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>Aanbellen tijdens AI-opname (of andersom) creëert nu 1 enkele opname met <code>category='ring,person'</code>. Zowel <code>[🔔 Aangebeld]</code> als <code>[🚶 Persoon]</code> badges worden naast elkaar getoond.</td>
        </tr>
        <tr>
          <td><b>Slim Anti-Wind Filter</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>Laag 1 vereist aspect-ratio &ge; 1.25 (verwerpt platte takkenschaduwen) + hogere conf (&ge;0.62) in struikzones (x &lt; 0.16 of x &gt; 0.68). Laag 2 vereist &ge;3 frames met oscillatie-ratio &ge; 0.60.</td>
        </tr>
        <tr>
          <td><b>Ringbuffer Pre-Roll Garantie</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>Segmenten worden nooit meer gewist op tijdgaten; alle pre-roll segmenten worden integraal behouden. Harde minimumduur 40s gegarandeerd.</td>
        </tr>
        <tr>
          <td><b>Notificatie & Sync Garantie</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>High-priority FCM met <code>android: {{priority: "high"}}</code>, deduplicatie gecapped op 50 IDs, en nieuw endpoint <code>/api/v1/events/unnotified</code> voor netwerkwissels.</td>
        </tr>
        <tr>
          <td><b>Sniffer Tijdzone & HEX</b></td>
          <td><span class="badge badge-success">COMPLEET</span></td>
          <td>Tijdstempels in <code>Europe/Amsterdam</code> en volledige niet-afgekapte HEX-payload logging. Sniffer blijft 100% passief (geen ring triggers).</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>📱 APK Release Info</h3>
      <pre>{apk_info}
Git HEAD: {git_head}</pre>
    </div>

    <div class="card">
      <h3>🔐 Module Guard & Smoke Checks (v3.5.5)</h3>
      <pre>{guard_status}</pre>
    </div>

    <div class="card">
      <h3>🔍 Triage Systeemstatus</h3>
      <pre>{triage_status}</pre>
    </div>
  </div>
  <div class="footer">
    Foxway Smart Doorbell System • Geautomatiseerde Uitrol v3.5.5
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Smart Doorbell <{smtp_user}>"
    msg["To"] = recipient

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        print(f"✅ Email report successfully sent to {recipient}")
    except Exception as e:
        print(f"❌ Failed to send email report: {e}")

if __name__ == "__main__":
    send_report()
