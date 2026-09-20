FROM cgr.dev/chainguard/wolfi-base@sha256:1d95114038f76513a9ace6fca107d5582b08c65981f81f61cb56bf7fd2ef216d AS build
RUN apk add --no-cache build-base=1-r9 pkgconf=3.0.7-r0 curl=8.22.0-r2 \
    openssl-dev=3.6.4-r7 libxml2-dev=2.15.4-r0 sqlite-dev=3.53.4-r2 \
    util-linux-dev=2.42.3-r4 libedit-dev=3.1-r19 jansson-dev=2.15.1-r1 \
    libxcrypt-dev=4.5.2-r5 linux-headers=7.2.6-r0 patch=2.8-r10 bash=5.3-r13 bzip2=1.0.8-r24
WORKDIR /build
RUN curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz -o source.tar.gz \
    && echo '3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94  source.tar.gz' | sha256sum -c - \
    && tar -xzf source.tar.gz
WORKDIR /build/asterisk-22.11.0
RUN ./configure --with-pjproject-bundled --without-dahdi --without-pri --without-radius --without-popt \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE menuselect.makeopts \
    && make -j2 \
    && make DESTDIR=/image install

FROM cgr.dev/chainguard/wolfi-base@sha256:1d95114038f76513a9ace6fca107d5582b08c65981f81f61cb56bf7fd2ef216d
RUN apk add --no-cache libssl3=3.6.4-r7 libxml2-16=2.15.4-r0 sqlite-libs=3.53.4-r2 \
    libuuid=2.42.3-r4 libedit=3.1-r19 jansson=2.15.1-r1 libxcrypt=4.5.2-r5 libstdc++=16.2.0-r1
COPY --from=build /image/usr/sbin/asterisk /usr/bin/asterisk
COPY --from=build /image/usr/lib/ /usr/lib/
COPY --from=build /image/var/lib/asterisk/ /var/lib/asterisk/
USER 10001:10001
ENTRYPOINT ["/usr/sbin/asterisk"]
CMD ["-f", "-C", "/etc/asterisk/asterisk.conf"]
