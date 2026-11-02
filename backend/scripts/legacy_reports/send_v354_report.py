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
    subject = "Foxway Smart Doorbell v3.5.4 (Build 37) - Sniffer Forensisch Rapport, Resilient Call Recording, AI Filter & UI Polish"

    # Status checks
    version_status = run_cmd("curl -s http://127.0.0.1:8090/api/v1/version")
    apk_info = run_cmd("ls -lh /mnt/opslag/deurbel/clips/foxway-deurbel.apk")
    git_head = run_cmd("git log -1 --oneline")
    guard_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/module_guard.py --verify")
    triage_status = run_cmd("python3 /mnt/opslag/deurbel/scripts/triage.py | head -45")
    clips_status = run_cmd("curl -s 'http://127.0.0.1:8090/api/v1/clips?limit=10' | jq '{count: length, newest: .[0].timestamp, oldest: .[-1].timestamp}'")

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
    <h1>🚀 Foxway Smart Doorbell v3.5.4 (Build 37)</h1>
    <p>Forensisch Sniffer Rapport • Autonome Call Recording • 12% Voertuigfilter • Camera Klok Sync • Huis-met-Bel Logo</p>
  </div>
  <div class="content">

    <div class="card">
      <h3>🔍 1. FORENSISCH RAPPORT: FYSIEKE AANBELTEST 2 NOVEMBER</h3>
      <table>
        <tr><th>Veld</th><th>Waarde</th></tr>
        <tr><td><strong>Exact Tijdstip Ring</strong></td><td>2026-11-02 07:51:32.633 UTC (08:51:32 CET)</td></tr>
        <tr><td><strong>Call ID</strong></td><td><code>c225c24b5bb7</code></td></tr>
        <tr><td><strong>Shazam Chime Evaluatie</strong></td><td>Eerste detectie: NCC=0.7784 (drempel=0.72) • Piek: NCC=0.8447 (728 Hz / 570 Hz dual-tone)</td></tr>
        <tr><td><strong>Passieve Sniffer Analyse</strong></td><td>Tijdens het 12.3-seconden ringvenster (07:51:28 - 07:51:41) verzond de camera 0 WAN-pakketten naar de cloud. Alleen periodieke TCP keepalives (191 bytes, poort 8820) naar 54.78.131.213 buiten het bellen. Geen unieke WAN HEX-match! Shazam Chime is de enige 100% betrouwbare ground-truth.</td></tr>
        <tr><td><strong>Herstel & Opname</strong></td><td>Opname hersteld in database en feed: <code>/mnt/opslag/deurbel/clips/2026-11-02/c225c24b5bb7.mp4</code> (duur: 48.2s, 6.1 MB, READY).</td></tr>
      </table>
    </div>

    <div class="card">
      <h3>📹 2. RESILIENT CALL RECORDING (NIET MEER AFGEBROKEN BIJ WEIGEREN)</h3>
      <ul>
        <li><strong>Directe T0 database registratie:</strong> Bij ring wordt direct synchroon een clip-rij met <code>status = "RECORDING"</code>, <code>category = "ring"</code> en miniatuur aangemaakt.</li>
        <li><strong>Autonome doorloop:</strong> Weigeren (<code>decline</code>), ophangen (<code>hangup</code>) of timeout op Android breekt de opname <strong>NOOIT</strong> meer af! Video blijft autonoom opnemen zolang er beweging is + post-roll.</li>
        <li><strong>Stream-copy Concat Fix:</strong> Toegevoegd <code>-fflags +genpts -avoid_negative_ts make_zero -movflags +faststart</code> waardoor 00:01 containerduur-bugs definitief zijn opgelost.</li>
      </ul>
    </div>

    <div class="card">
      <h3>🚗 3. ELIMINATIE VALSE KORTE OPNAMES & 12% VERPLAATSINGSREGEL</h3>
      <ul>
        <li><strong>Strict Vehicle Passing Gatekeeper:</strong> Minstens 3 scans (<code>len(frames) &ge; 3</code>) én &ge; 12% schermbreedte horizontale verplaatsing (<code>dx &ge; 0.12 * w</code>) in consistente richting.</li>
        <li><strong>Parked Car Memory Overlap:</strong> Voertuigen met &ge; 0.50 IoU overlap met stilstaande auto's worden direct genegeerd.</li>
        <li><strong>Contour Motion Fallback Uit:</strong> Domme pixel/contour fallback zonder YOLO detectie volledig uitgeschakeld.</li>
        <li><strong>Database Opschoning:</strong> 18 valse korte opnames (en 165 verweesde fragmenten, 24.6 MB) opgeruimd.</li>
      </ul>
    </div>

    <div class="card">
      <h3>🕒 4. CAMERA HARDWARE KLOK SYNC</h3>
      <ul>
        <li><strong>C-SDK NET_DVR_SET_TIMECFG (cmd 119):</strong> Camera hardwareklok (192.168.2.31) synchroniseert direct met lokale servertijd (Europe/Amsterdam).</li>
        <li><strong>Automatisch & Periodiek:</strong> Direct bij verbinden op Poort 8000 en elk uur via health monitor.</li>
      </ul>
    </div>

    <div class="card">
      <h3>🎨 5. UNIFORM HUIS-MET-BEL LOGO & DASHBOARD FIXES</h3>
      <ul>
        <li><strong>App Icoon & Logo:</strong> Nieuw haarscherp vector-icoon <code>ic_foxway_house_bell.xml</code> (modern huisje met gecentreerde bel in Foxway groen <code>#10B981</code>) als launcher-icoon en status-bar icoon.</li>
        <li><strong>Web Admin & Login:</strong> Bell emoji 🔔 vervangen door het bijpassende SVG huis-met-bel vector in <code>admin.html</code> en <code>login.html</code>.</li>
        <li><strong>/admin Dashboard Hersteld:</strong> Dubbele catch-blok syntaxfout verholpen; <code>node --check</code> slaagt 100%; meters tonen 50.0 GB opslag; alle 4 tabbladen schakelen soepel.</li>
      </ul>
    </div>

    <div class="card">
      <h3>🛡️ 6. VERIFICATIE & SMOKE CHECKS</h3>
      <p><strong>Module Guard Verificatie:</strong></p>
      <pre>{guard_status}</pre>
      <p><strong>APK Details:</strong></p>
      <pre>{apk_info}</pre>
      <p><strong>Laatste Git Commit:</strong></p>
      <pre>{git_head}</pre>
    </div>

  </div>
  <div class="footer">
    Foxway Smart Doorbell System • Server: foxway (192.168.2.1)
  </div>
</div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Deurbel <{smtp_user}>"
    msg["To"] = recipient

    part = MIMEText(html_content, "html")
    msg.attach(part)

    print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        print(f"✅ Report successfully sent to {recipient}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

if __name__ == "__main__":
    send_report()

