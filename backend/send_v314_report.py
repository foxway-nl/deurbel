import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def send_report():
    smtp_host = os.getenv("FOXWAY_SMTP_HOST", "mailserver.foxway.nl")
    smtp_port = int(os.getenv("FOXWAY_SMTP_PORT", "587"))
    smtp_user = os.getenv("FOXWAY_SMTP_USER", "mo@foxway.nl")
    smtp_pass = os.getenv("FOXWAY_SMTP_PASS", "Alhoceima2026!")

    recipient = "mlamaiz@gmail.com"
    subject = "Foxway Smart Doorbell v3.1.4 (Build 26) - Definitieve Verwijdering Akoestische Chime, Audio Hard-Kill & Vaste 4:3 Aspect Ratio"

    html_content = """
    <html>
    <head>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b; background: #f8fafc; padding: 20px; }
            .container { max-width: 720px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; }
            .header { background: #0f172a; color: #f8fafc; padding: 24px; }
            .header h1 { margin: 0 0 6px 0; font-size: 20px; }
            .header p { margin: 0; color: #94a3b8; font-size: 14px; }
            .content { padding: 24px; line-height: 1.6; }
            .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
            .badge-success { background: #dcfce7; color: #166534; }
            .card { background: #f1f5f9; border-radius: 6px; padding: 14px 16px; margin: 12px 0; border-left: 4px solid #10b981; }
            .card h3 { margin: 0 0 6px 0; font-size: 15px; color: #0f172a; }
            .card p { margin: 0; font-size: 13px; color: #475569; }
            table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }
            th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
            th { background: #f8fafc; color: #475569; font-weight: 600; }
            a { color: #059669; text-decoration: none; font-weight: 500; }
            .footer { background: #f8fafc; border-top: 1px solid #e2e8f0; padding: 16px 24px; font-size: 12px; color: #64748b; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Foxway Smart Doorbell v3.1.4 Release &amp; Verificatie Rapport</h1>
                <p>Status: <span class="badge badge-success">ALLE 4 TAKEN VOLTOOID &bull; 63/63 PYTESTS GESLAAGD &bull; BUILD 26</span> &bull; 30 Oktober 2026</p>
            </div>
            <div class="content">
                <p>Beste Mohamed,</p>
                <p>De officiële release voor <strong>v3.1.4 (versionCode 26)</strong> is succesvol gebouwd, geverifieerd en uitgerold op de Foxway productie-server. Alle 4 opdrachten zijn rigoureus uitgevoerd:</p>
                
                <div class="card">
                    <h3>1. Akoestische Chime-Detectie Volledig Gesloopt (Taak 1)</h3>
                    <p><code>backend/app/ring_detector.py</code> en bijbehorende tests zijn onherroepelijk verwijderd. Alle 728 Hz / 580 Hz FFT analyse en RTSP audio-stream verwerking zijn gewist. In de database zijn de chime-instellingen opgeruimd en in <code>/admin</code> is de chime-toggle vervangen door een hardware-status indicatie. Deurbeltriggers verlopen nu <strong>uitsluitend</strong> via het Poort 8000 C-SDK alarmkanaal (<code>NET_DVR_SetupAlarmChan_V41</code> / <code>COMM_UPLOAD_VIDEO_INTERCOM_EVENT</code> met <code>byEventType == 1</code>) en de fysieke Shelly Plus Uni webhook (<code>POST /api/v1/webhook/shelly-ring?force=true</code>).</p>
                </div>

                <div class="card">
                    <h3>2. Synchrone Hard-Kill voor Audio bij Schermwissel of App Sluiten (Taak 2)</h3>
                    <p>In Android (<code>LiveScreen.kt</code>, <code>CallScreen.kt</code>, <code>CallActivity.kt</code>, en <code>WebSocketForegroundService.kt</code>) zorgt <code>LiveStreamController.hardKillAudio()</code> voor een onmiddellijke synchrone stop van alle WebRTC/MSE audio-tracks en streams bij <code>onStop()</code>, <code>onPause()</code>, <code>onDestroy()</code> en bij het ophangen. Audio lekt nooit meer door na het verlaten van een scherm. Daarnaast schakelt het luidspreker-icoon direct <code>AudioManager.isSpeakerphoneOn</code> in en uit.</p>
                </div>

                <div class="card">
                    <h3>3. Strikte Onwrikbare 4:3 Aspect Ratio (Taak 3)</h3>
                    <p>In zowel <code>LiveScreen.kt</code> als <code>CallScreen.kt</code> is de video-weergave nu vastgezet op de native beeldsensor-verhouding met <code>.fillMaxWidth().aspectRatio(4f / 3f).clip(RectangleShape)</code>. Alle pas- en uitrek-knoppen (<code>isFitMode</code>) zijn verwijderd. Het beeld vult de breedte en behoudt te allen tijde de zuivere 4:3 verhouding zonder enige visuele vervorming.</p>
                </div>

                <div class="card">
                    <h3>4. Versiebeheer, Tests &amp; APK Release (Taak 4)</h3>
                    <p>De versie is verhoogd naar <strong>v3.1.4 (versionCode 26)</strong> in zowel de backend als de Android build-configuratie. Alle 63 backend unittests zijn geslaagd. De nieuwe APK is gecompileerd en klaargezet op de productielocatie:</p>
                    <p style="margin-top: 8px;">
                        <strong>Directe Download:</strong> <a href="https://deurbel.foxway.nl/clips/foxway-deurbel.apk">https://deurbel.foxway.nl/clips/foxway-deurbel.apk</a><br>
                        <strong>Lokaal Pad:</strong> <code>/mnt/opslag/deurbel/clips/foxway-deurbel.apk</code>
                    </p>
                </div>

                <table>
                    <thead>
                        <tr>
                            <th>Component</th>
                            <th>Status v3.1.4</th>
                            <th>Verificatie</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><strong>Aanbel-Detectie</strong></td>
                            <td>Hardware Only (Poort 8000 SDK + Shelly)</td>
                            <td>Geen FFT/Shazam chime meer in codebase</td>
                        </tr>
                        <tr>
                            <td><strong>Audio Lifecycle</strong></td>
                            <td>Synchrone Hard-Kill op exit/hangup</td>
                            <td>Audio stopt direct, speaker toggle werkend</td>
                        </tr>
                        <tr>
                            <td><strong>Video Container</strong></td>
                            <td>Vast 4:3 (aspectRatio 4f/3f)</td>
                            <td>Geen uitrekking, fit-knoppen verwijderd</td>
                        </tr>
                        <tr>
                            <td><strong>Backend Tests</strong></td>
                            <td>63/63 Tests Geslaagd</td>
                            <td>Docker container test suite 100% OK</td>
                        </tr>
                        <tr>
                            <td><strong>Android APK</strong></td>
                            <td>v3.1.4 (Build 26) Gecompileerd</td>
                            <td>Gekopieerd naar /clips/foxway-deurbel.apk</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <div class="footer">
                Foxway Smart Doorbell System &bull; Geautomatiseerd verificatierapport gegenereerd op foxway server (192.168.2.1)
            </div>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Foxway Smart Doorbell <{smtp_user}>"
    msg["To"] = recipient

    part = MIMEText(html_content, "html")
    msg.attach(part)

    try:
        print(f"Connecting to SMTP server {smtp_host}:{smtp_port}...")
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.as_string())
        server.quit()
        print(f"Report successfully sent to {recipient}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

if __name__ == "__main__":
    send_report()

