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
    subject = "Foxway Smart Doorbell v3.5.0 (Build 32) - Dual Aanbel-Coördinator, Arlo-UI & Dag-Mappen"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    storage_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/settings/storage")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; color: #2d3748; padding: 20px; }}
  .container {{ max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.07); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #0f766e, #0d9488); color: white; padding: 28px; }}
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
    <h1>🚀 Foxway Smart Doorbell v3.5.0 (Build 32)</h1>
    <p>Dual Aanbel-Coördinator • Arlo-Stijl Timeline UI • Dag-Mappen (/clips/YYYY-MM-DD/) • 50 GB Opslag</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>📋 Overzicht van Uitgevoerde Taken in v3.5.0</h3>
      <table>
        <tr><th>Onderdeel</th><th>Status</th><th>Beschrijving</th></tr>
        <tr>
          <td><b>Dual Aanbel-Coördinator</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Directe ms 0 trigger bij 1e signaal (Shazam Chime of Cloud Sniffer). 3.5s verificatievenster logt [RING VERIFIED] of [RING MISMATCH ERROR].</td>
        </tr>
        <tr>
          <td><b>EZVIZ Cloud Sniffer</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Onderdrukt 191B keepalives op TCP 8820; detecteert Patroon A (payload 195-400B) en Patroon B (&ge;3 pkts van 191B binnen 1s).</td>
        </tr>
        <tr>
          <td><b>Dag-Mappen & Opslaglimiet</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Clips en thumbnails direct in /clips/YYYY-MM-DD/. Opslaglimiet verhoogd naar 50.0 GB (51200 MB). Lege mappen opgeruimd bij delete.</td>
        </tr>
        <tr>
          <td><b>Server-Side Paginering</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>GET /api/v1/clips past SQL-filtering toe vóór limit en offset. Android app gebruikt 40 items per pagina met oneindige scroll.</td>
        </tr>
        <tr>
          <td><b>Arlo-Stijl UI (Android)</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Klikbare thumbnail fungeert als Play-knop met overlay en duur-badge. 3 compacte knoppen: Download, Ster (⭐), Delete (🗑️). Schone 🔔 Aangebeld badge.</td>
        </tr>
        <tr>
          <td><b>Web Admin Verbeteringen</b></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Dual Aanbel live status, werkende AI Inspector snapshots (/api/v1/admin/inspector-snapshot/{{log_id}}) en Test Push Melding knop.</td>
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
      <h3>💾 Opslag Status (50 GB Cap)</h3>
      <pre>{storage_status}</pre>
    </div>

    <div class="card">
      <h3>🔄 Git Release Commit</h3>
      <pre>{git_head}</pre>
    </div>

  </div>
  <div class="footer">
    Foxway Smart Doorbell System &bull; Versie 3.5.0 &bull; Automatisch gegenereerd rapport
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Deurbel <{smtp_user}>"
    msg["To"] = recipient

    part = MIMEText(html_content, "html")
    msg.attach(part)

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.as_string())
        server.quit()
        print(f"✅ E-mail rapport succesvol verzonden naar {recipient} via {smtp_host}:{smtp_port}")
    except Exception as e:
        print(f"❌ Fout bij verzenden e-mail via {smtp_host}: {e}")

if __name__ == "__main__":
    send_report()
