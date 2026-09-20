# P19 Kamailio source-build foundation

This image is an implementation-in-progress build foundation, not a certified SIP
edge or production deployment. The routing configuration and protocol fixtures
are not yet complete. Startup requires an explicit `/etc/nxs/kamailio.cfg`; the
image does not use the upstream example routing configuration as a safe default.

The official Kamailio 6.1.4 source archive is pinned to its publisher's SHA-256
`290624b6624edc230af0fc458fb7e481e64b893be7cb5084398e587942992e0a`.
Both stages pin the official Debian bookworm multiarchitecture index and use a
dated, signed Debian package snapshot. Architecture builds compile the same
source independently. An available base-image architecture is not build/test
certification; successful image builds and protocol runs must be recorded later.

The image runs as UID/GID 10001. Reference test deployments must drop capabilities,
use no-new-privileges, avoid host networking/PID/Docker socket, and provide only
necessary writable temporary filesystems. No database or carrier credential is
embedded. No media proxy, automatic failover or capacity claim is introduced.

Source and checksum: <https://www.kamailio.org/pub/kamailio/6.1.4/src/>.
