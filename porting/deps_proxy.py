"""Explicit HTTPS-only dependency access; never a general application proxy."""
import ipaddress
import os
import re
import sys
import tempfile


DEFAULT_HOSTS = (
    "github.com api.github.com codeload.github.com raw.githubusercontent.com "
    "objects.githubusercontent.com release-assets.githubusercontent.com "
    "packagist.org repo.packagist.org getcomposer.org"
)
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"{LABEL}(?:\.{LABEL})+", re.ASCII)


def allowed_hosts(value):
    hosts = value.lower().split()
    if not hosts or len(hosts) > 100:
        raise ValueError("Set 1 to 100 exact public dependency hostnames.")
    for host in hosts:
        if len(host) > 253 or not HOST.fullmatch(host):
            raise ValueError("Dependency hosts must be exact DNS names, without URLs or wildcards.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals are not dependency hostnames.")
    return sorted(set(hosts))


def configuration(hosts):
    # Deny private/reserved destinations even when an allowlisted hostname
    # resolves to one. -n prevents reverse-DNS matching of IP-literal requests.
    return """http_port 3128
visible_hostname warexo-dependencies
acl tls_port port 443
acl dependency_hosts dstdomain -n %s
acl private_destinations dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8
acl private_destinations dst 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24
acl private_destinations dst 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24
acl private_destinations dst 224.0.0.0/4 240.0.0.0/4 ::/128 ::1/128
acl private_destinations dst 64:ff9b::/96 2001:db8::/32 fc00::/7 fe80::/10 fec0::/10 ff00::/8
http_access deny !CONNECT
http_access deny !tls_port
http_access deny !dependency_hosts
http_access deny private_destinations
http_access allow CONNECT tls_port dependency_hosts
http_access deny all
cache deny all
access_log none
cache_log /dev/stderr
logfile_rotate 0
pid_filename /tmp/warexo-squid.pid
coredump_dir /tmp
pinger_enable off
shutdown_lifetime 1 seconds
""" % " ".join(hosts)


def main():
    try:
        hosts = allowed_hosts(os.environ.get("PORTING_ALLOWED_HOSTS", DEFAULT_HOSTS))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    config = configuration(hosts)
    if sys.argv[1:] == ["--print-config"]:
        print(config, end="")
        return 0
    if sys.argv[1:]:
        print("Usage: deps_proxy.py [--print-config]", file=sys.stderr)
        return 2
    fd, path = tempfile.mkstemp(prefix="warexo-squid-", suffix=".conf")
    with os.fdopen(fd, "w") as stream:
        stream.write(config)
    os.execv("/usr/sbin/squid", ["squid", "-N", "-f", path])


if __name__ == "__main__":
    sys.exit(main())
