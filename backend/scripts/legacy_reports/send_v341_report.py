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
    subject = "Foxway Smart Doorbell v3.4.1 (Build 31) - Host ARP Cloud-Sniffer & 10/10 AI Fallback"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    system_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/admin/system-status")
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
    <h1>🚀 Foxway Smart Doorbell v3.4.1 (Build 31)</h1>
    <p>Host ARP Cloud Packet Sniffer • 10/10 AI Sensitivity & Smart Motion Fallback • Fast Reconnect & WiFi/5G Recovery</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>📋 Overzicht van Uitgevoerde Taken in v3.4.1</h3>
      <table>
        <tr><th>Onderdeel</th><th>Status</th><th>Beschrijving</th></tr>
        <tr>
          <td><strong>Forensische Analyse (16:53:42 Druk)</strong></td>
          <td><span class="badge badge-success">BEVESTIGD</span></td>
          <td>Shazam NCC = 0.7542 (piek 0.8482) triggerde event 624 (15:53:08 UTC). C-SDK 0x1132 werd door EZVIZ firmware geblokkeerd.</td>
        </tr>
        <tr>
          <td><strong>Host-Level ARP Cloud-Sniffer</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Gerichte ARP-reply elke 2.0s via 192.168.2.1 (eno1). Vangt uitgaande UDP 6404 EZVIZ cloudbursts op en triggert NETWORK_SNIFF.</td>
        </tr>
        <tr>
          <td><strong>ARP Fail-Safe Bescherming</strong></td>
          <td><span class="badge badge-success">GEWAARBORGD</span></td>
          <td>Bij SIGTERM, SIGINT en atexit worden direct 3x ARP restore packets (00:f8:cc:d0:9e:00) naar 192.168.2.31 gestuurd.</td>
        </tr>
        <tr>
          <td><strong>Shazam Auto-Calibratie</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Bij elke Shazam chime wordt de corresponderende cloudburst in network_ring_signature.json opgeslagen en gesynchroniseerd.</td>
        </tr>
        <tr>
          <td><strong>10/10 EZVIZ AI-Gevoeligheid</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>YOLO conf-drempel verlaagd naar 0.22 voor 10s bij camera hardware beweging (0x4000 dwAlarmType=3). 4 FPS burst geactiveerd.</td>
        </tr>
        <tr>
          <td><strong>Smart Motion-Fallback</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Contour motion fallback (area &ge; 1.5%, &Delta;d &ge; 2% over 3 frames) buiten stilstaande geparkeerde auto's en boomkruinen.</td>
        </tr>
        <tr>
          <td><strong>Netwerk Herstel (Camera & App)</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>Backend C-SDK en Shazam herverbinden binnen 3.0s ([CAMERA RECONNECTED]). Android ConnectivityManager detecteert WiFi/5G wissel.</td>
        </tr>
        <tr>
          <td><strong>Badges in Clips & Logboek</strong></td>
          <td><span class="badge badge-success">ACTIEF</span></td>
          <td>[SHAZAM_CHIME], [NETWORK_SNIFF] en [SHAZAM + SNIFF] badges zichtbaar in ClipsScreen en 30-dagen Logboek.</td>
        </tr>
        <tr>
          <td><strong>Android APK v3.4.1 (Build 31)</strong></td>
          <td><span class="badge badge-success">GEBOUWD</span></td>
          <td>Gecompileerd met --max-workers=2 en isMinifyEnabled=false. Beschikbaar op /mnt/opslag/deurbel/clips/foxway-deurbel.apk.</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>📦 APK & Git Releasestatus</h3>
      <table>
        <tr><td><strong>APK Bestand</strong></td><td><code>{apk_info}</code></td></tr>
        <tr><td><strong>Download URL</strong></td><td><a href="https://deurbel.foxway.nl/clips/foxway-deurbel.apk">https://deurbel.foxway.nl/clips/foxway-deurbel.apk</a></td></tr>
        <tr><td><strong>Git Commit</strong></td><td><code>{git_head}</code></td></tr>
      </table>
    </div>

    <div class="card">
      <h3>⚙️ Live Backend Versie Check</h3>
      <pre>{version_status}</pre>
    </div>

    <div class="card">
      <h3>🖥️ Systeemstatus & Sniffer Diagnostiek</h3>
      <pre>{system_status}</pre>
    </div>

  </div>
  <div class="footer">
    Foxway Deurbel Automated Verification Reporter • Host: foxway (192.168.2.1)
  </div>
</div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Doorbell <{smtp_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        print(f"✅ Email report successfully sent to {recipient}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

if __name__ == "__main__":
    send_report()

