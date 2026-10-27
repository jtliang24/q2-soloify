/* The wire's framing (docs/wire.md): a type byte, a big-endian u32 length, the payload. */
#include "relay.h"

uint32_t be32(const unsigned char *p) { return (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]; }

void put_be32(unsigned char *p, uint32_t v) {
    p[0] = v >> 24, p[1] = v >> 16, p[2] = v >> 8, p[3] = v;
}

int wire_send(struct wire *w, char type, const void *payload, int n) {
    if (w->nout + 5 + n > (int)sizeof w->out) return -1;
    w->out[w->nout] = type;
    put_be32(w->out + w->nout + 1, n);
    memcpy(w->out + w->nout + 5, payload, n);
    w->nout += 5 + n;
    return 0;
}

int wire_next(struct wire *w, const unsigned char **payload, int *n) {
    uint32_t len;
    if (w->nin < 5) return 0;
    len = be32(w->in + 1);
    if (len > sizeof w->in - 5) return -1;
    if (w->nin < 5 + (int)len) return 0;
    *payload = w->in + 5;
    *n = len;
    return w->in[0];
}

void wire_consume(struct wire *w) {
    int size = 5 + be32(w->in + 1);
    memmove(w->in, w->in + size, w->nin - size);
    w->nin -= size;
}
