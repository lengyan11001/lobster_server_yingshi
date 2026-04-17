"""Verify skill store content on server."""
import json
import paramiko

NEW = {"host": "42.194.209.150", "user": "ubuntu", "password": "|EP^q4r5-)f2k", "dir": "/opt/lobster-server"}

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(NEW["host"], username=NEW["user"], password=NEW["password"],
          timeout=15, look_for_keys=False, allow_agent=False)

def run(cmd, timeout=30):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace").strip()

# Read the actual file
out = run(f"cat {NEW['dir']}/skill_registry.json")
registry = json.loads(out)
packages = registry.get("packages", {})

print(f"Total packages in registry: {len(packages)}")
print()

target_skills = ["comfly_veo_skill", "comfly_ecommerce_detail_skill", "ecommerce_publish_skill"]

for pkg_id, pkg in packages.items():
    vis = pkg.get("store_visibility", "(not set => debug)")
    show = pkg.get("show_in_store", True)
    marker = " <<< NEW" if pkg_id in target_skills else ""
    print(f"  {pkg_id:40s} | vis={str(vis):10s} | show={show}{marker}")

# Check that services read the file (check backend log for startup)
print()
out = run("sudo journalctl -u lobster-backend --since '2 minutes ago' --no-pager -n 20 2>&1")
print("=== Recent backend logs ===")
print(out[:1500])

c.close()
