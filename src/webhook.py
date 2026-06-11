
import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer


class SmartHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)

        try:
            # Parse the incoming JSON payload from Alertmanager
            payload = json.loads(post_data.decode("utf-8"))
            status = payload.get("status", "unknown")

            # Only act if the alert is actively firing
            if status == "firing":
                for alert in payload.get("alerts", []):
                    alert_name = alert.get("labels", {}).get(
                        "alertname", "Unknown"
                    )
                    instance = alert.get("labels", {}).get(
                        "instance", "Unknown"
                    )

                    print(
                        f"🚨 ALERT TRIGGERED: {alert_name} on {instance} is Firing!"
                    )

                    if alert_name == "WebServerDown":
                        print("🤖 Remediation Core: Launching Ansible Playbook...")
                        subprocess.run([
                            "ansible-playbook",
                            "-i",
                            "/vagrant/ansible/inventory.ini",
                            "/vagrant/src/restart-nginx.yml",
                        ])

            elif status == "resolved":
                print(
                    "✅ ALERT RESOLVED: Infrastructure stabilized. Standing down."
                )

        except Exception as e:
            print(f"❌ Webhook Error: Failed to process payload: {e}")

        self.send_response(200)
        self.end_headers()


server = HTTPServer(("0.0.0.0", 5001), SmartHandler)
print("🚀 Smart Webhook Server running on port 5001...")
server.serve_forever()