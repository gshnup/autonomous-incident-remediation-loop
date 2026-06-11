from http.server import BaseHTTPRequestHandler, HTTPServer
import subprocess

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        print("ALERT RECEIVED")

        subprocess.run([
            "ansible-playbook",
            "-i",
            "/home/vagrant/self-healing/inventory.ini",
            "/home/vagrant/self-healing/restart-nginx.yml"
        ])

        self.send_response(200)
        self.end_headers()

server = HTTPServer(("0.0.0.0", 5001), Handler)

print("Webhook listening on port 5001")

server.serve_forever()
