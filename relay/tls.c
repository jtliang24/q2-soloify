/* The link to the bridge: a non-blocking TCP connection with a short timeout, then TLS through the
 * rootfs's OpenSSL 1.1.1f. With a pin, only the bridge's own certificate is accepted and nothing
 * else is checked, so the device's clock doesn't matter. Without one, the chain is checked against
 * Q2 Pod's CA bundle and the name against the host. */
#include "relay.h"

#define CA_FILE "/etc/scrobble-ca.pem"
#define LAN_CONNECT_MS 1500
#define WAN_CONNECT_MS 5000
#define HANDSHAKE_MS 5000

static int dial(const struct endpoint *ep) {
    struct addrinfo hints, *res, *a;
    int fd = -1, on = 1, idle = 5, intvl = 2, cnt = 3;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(ep->host, ep->port, &hints, &res)) {
        say("%s: can't resolve", ep->host);
        return -1;
    }
    for (a = res; a; a = a->ai_next) {
        uint64_t deadline = now_ms() + (ep->lan ? LAN_CONNECT_MS : WAN_CONNECT_MS);
        int err = 0;
        socklen_t len = sizeof err;
        fd = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
        if (fd < 0) continue;
        fcntl(fd, F_SETFD, FD_CLOEXEC);
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
        if (connect(fd, a->ai_addr, a->ai_addrlen) < 0 && relay_errno != EINPROGRESS) err = relay_errno;
        else if (!(relay_wait(fd, POLLOUT, deadline) & (POLLOUT | POLLERR | POLLHUP))) err = -1;
        else getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
        if (!err) break;
        say("%s:%s: %s", ep->host, ep->port, err < 0 ? "connect timed out" : "can't connect");
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd >= 0) {
        /* A dead link (a network switch) shows within about 11 s even with nothing to send */
        setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof on);
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle);
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl);
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof on);
    }
    return fd;
}

static int pinned_ok(SSL *ssl, const unsigned char *pin) {
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned n = 0;
    X509 *cert = SSL_get_peer_certificate(ssl);
    int ok = cert && X509_digest(cert, EVP_sha256(), md, &n) && n == 32 && !memcmp(md, pin, 32);
    if (cert) X509_free(cert);
    return ok;
}

int link_open(struct link *l, const struct endpoint *ep) {
    uint64_t deadline;
    const char *ca = getenv("SOLOIFY_CA");
    memset(l, 0, sizeof *l);
    if ((l->fd = dial(ep)) < 0) return -1;
    l->ctx = SSL_CTX_new(TLS_client_method());
    if (!l->ctx) goto fail;
    SSL_CTX_set_min_proto_version(l->ctx, TLS1_2_VERSION);
    SSL_CTX_set_mode(l->ctx, SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
    if (ep->pinned) {
        SSL_CTX_set_verify(l->ctx, SSL_VERIFY_NONE, NULL);
    } else {
        SSL_CTX_set_verify(l->ctx, SSL_VERIFY_PEER, NULL);
        if (SSL_CTX_load_verify_locations(l->ctx, ca ? ca : CA_FILE, NULL) != 1) {
            say("can't read the CA bundle");
            goto fail;
        }
    }
    if (!(l->ssl = SSL_new(l->ctx)) || SSL_set_fd(l->ssl, l->fd) != 1) goto fail;
    if (!ep->pinned && (!SSL_set_tlsext_host_name(l->ssl, ep->host) || SSL_set1_host(l->ssl, ep->host) != 1))
        goto fail;
    for (deadline = now_ms() + HANDSHAKE_MS;;) {
        int r, e;
        ERR_clear_error();
        if ((r = SSL_connect(l->ssl)) == 1) break;
        e = SSL_get_error(l->ssl, r);
        if ((e != SSL_ERROR_WANT_READ && e != SSL_ERROR_WANT_WRITE) ||
            !relay_wait(l->fd, e == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, deadline)) {
            say("%s: TLS handshake failed", ep->host);
            goto fail;
        }
    }
    if (ep->pinned ? !pinned_ok(l->ssl, ep->pin) : SSL_get_verify_result(l->ssl) != X509_V_OK) {
        say("%s: not the bridge's certificate", ep->host);
        goto fail;
    }
    return 0;
fail:
    link_close(l);
    return -1;
}

int link_read(struct link *l, void *buf, int n) {
    int r, e;
    ERR_clear_error();
    if ((r = SSL_read(l->ssl, buf, n)) > 0) return r;
    e = SSL_get_error(l->ssl, r);
    return e == SSL_ERROR_WANT_READ || e == SSL_ERROR_WANT_WRITE ? 0 : -1;
}

int link_write(struct link *l, const void *buf, int n) {
    int r, e;
    ERR_clear_error();
    if ((r = SSL_write(l->ssl, buf, n)) > 0) return r;
    e = SSL_get_error(l->ssl, r);
    return e == SSL_ERROR_WANT_READ || e == SSL_ERROR_WANT_WRITE ? 0 : -1;
}

void link_close(struct link *l) {
    if (l->ssl) SSL_free(l->ssl);
    if (l->ctx) SSL_CTX_free(l->ctx);
    if (l->fd >= 0) close(l->fd);
    l->ssl = NULL, l->ctx = NULL, l->fd = -1;
}
