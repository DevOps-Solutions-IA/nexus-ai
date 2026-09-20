FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS build
RUN rm /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260918T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential bison flex pkg-config ca-certificates curl libssl-dev python3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    https://www.kamailio.org/pub/kamailio/6.1.4/src/kamailio-6.1.4_src.tar.gz -o source.tar.gz \
    && echo '290624b6624edc230af0fc458fb7e481e64b893be7cb5084398e587942992e0a  source.tar.gz' | sha256sum -c - \
    && tar -xzf source.tar.gz
WORKDIR /build/kamailio-6.1.4
RUN make cfg group_include=nxs_reference include_modules='sl tm tmx rr pv maxfwd sanity textops textopsx xlog corex kex ctl dialog htable topoh tls app_python3' \
    && make -j2 all \
    && make DESTDIR=/image install

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN rm /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260918T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends libssl3 libpython3.11 python3-minimal ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 nxs-sip \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin nxs-sip
COPY --from=build /image/usr/local/ /usr/local/
USER 10001:10001
ENTRYPOINT ["/usr/local/sbin/kamailio"]
CMD ["-DD", "-E", "-f", "/etc/nxs/kamailio.cfg", "-m", "64", "-M", "8"]
