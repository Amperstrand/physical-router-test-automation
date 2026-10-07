import socket, subprocess, time
SOCK = "/root/tollgate-virtual-lab/run/serial.sock"
pub = subprocess.run(["cat", "/home/ubuntu/.ssh/id_ed25519.pub"],
                     capture_output=True, text=True).stdout.strip()
parts = pub.split()
keyline = parts[0] + " " + parts[1]
chunks = [keyline[i:i+90] for i in range(0, len(keyline), 90)]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(SOCK); s.settimeout(2.0)
def drain(t=1.2):
    buf=b""; end=time.time()+t
    while time.time()<end:
        try:
            c=s.recv(4096)
            if not c: break
            buf+=c; end=time.time()+0.3
        except socket.timeout: pass
    return buf.decode(errors="replace")
def send(line=""):
    s.sendall(line.encode()+b"\n")
drain(2.0); s.sendall(b"\x03"); time.sleep(0.4); send(""); drain(1.0)
send("rm -f /tmp/k"); drain(0.4)
for ch in chunks:
    send(f"printf %s '{ch}' >> /tmp/k"); time.sleep(0.4)
send("echo; cat /tmp/k >> /etc/dropbear/authorized_keys; wc -l /etc/dropbear/authorized_keys")
time.sleep(2.5)
out = drain(3.0)
print([l for l in out.splitlines() if "authorized_keys" in l or l.strip().isdigit()][-2:])
