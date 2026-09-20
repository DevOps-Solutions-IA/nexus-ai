FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS build
RUN rm /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260918T000000Z trixie main' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential bison flex pkg-config ca-certificates curl libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    https://www.kamailio.org/pub/kamailio/6.1.4/src/kamailio-6.1.4_src.tar.gz -o source.tar.gz \
    && echo '290624b6624edc230af0fc458fb7e481e64b893be7cb5084398e587942992e0a  source.tar.gz' | sha256sum -c - \
    && tar -xzf source.tar.gz
WORKDIR /build/kamailio-6.1.4
RUN make cfg group_include=nxs_reference include_modules='sl tm tmx rr pv maxfwd sanity textops textopsx xlog corex kex ctl dialog htable topoh tls debugger app_python3' \
    && make -j2 all \
    && make DESTDIR=/image install

FROM cgr.dev/chainguard/python@sha256:1f37785e5cdb70151f36aaa15e1e3cef4571424dbefbf4b0d8a9222535cb13ff
COPY --from=build /image/usr/local/sbin/kamailio /usr/local/sbin/kamailio
COPY --from=build /image/usr/local/lib64/kamailio/ /usr/local/lib/kamailio/
USER 10001:10001
ENTRYPOINT ["/usr/local/sbin/kamailio"]
CMD ["-DD", "-E", "-Y", "/tmp/kamailio", "-f", "/etc/nxs/kamailio.cfg", "-m", "64", "-M", "8"]
