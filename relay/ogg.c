/* The decoder's input: one Ogg Opus stream, one page per packet, written by the relay so the same
 * stream runs across the bridge's encoders and a decoder can start at any packet. */
#include "relay.h"

#define SERIAL 0x51324f53u

static uint32_t crc_table[256];

static uint32_t crc(const unsigned char *p, int n, uint32_t c) {
    while (n--) c = c << 8 ^ crc_table[(c >> 24 ^ *p++) & 0xff];
    return c;
}

static int page(unsigned char *out, int flags, uint64_t granule, uint32_t seq, const unsigned char *data, int n) {
    int segs = n / 255 + 1, i;
    unsigned char *h = out;
    memcpy(h, "OggS", 4);
    h[4] = 0, h[5] = flags;
    for (i = 0; i < 8; ++i) h[6 + i] = granule >> 8 * i;
    for (i = 0; i < 4; ++i) h[14 + i] = SERIAL >> 8 * i, h[18 + i] = seq >> 8 * i, h[22 + i] = 0;
    h[26] = segs;
    for (i = 0; i < segs - 1; ++i) h[27 + i] = 255;
    h[27 + segs - 1] = n % 255;
    memcpy(h + 27 + segs, data, n);
    n += 27 + segs;
    uint32_t c = crc(out, n, 0);
    for (i = 0; i < 4; ++i) h[22 + i] = c >> 8 * i;
    return n;
}

void ogg_init(void) {
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t c = i << 24;
        for (int k = 0; k < 8; ++k) c = c & 0x80000000u ? c << 1 ^ 0x04c11db7u : c << 1;
        crc_table[i] = c;
    }
}

int ogg_headers(struct ogg *o, unsigned char *out, unsigned skip) {
    static const char vendor[] = "q2-soloify";
    unsigned char head[19] = {'O', 'p', 'u', 's', 'H', 'e', 'a', 'd', 1, 2, skip & 0xff, skip >> 8,
                              0x80, 0xbb, 0, 0, 0, 0, 0};
    unsigned char tags[8 + 4 + sizeof vendor - 1 + 4] = {'O', 'p', 'u', 's', 'T', 'a', 'g', 's', sizeof vendor - 1};
    int n;
    memcpy(tags + 12, vendor, sizeof vendor - 1);
    o->seq = 0, o->granule = 0;
    n = page(out, 2, 0, o->seq++, head, sizeof head);
    return n + page(out + n, 0, 0, o->seq++, tags, sizeof tags);
}

int ogg_packet(struct ogg *o, unsigned char *out, const unsigned char *p, int n, unsigned samples, int end) {
    o->granule += samples;
    return page(out, end ? 4 : 0, o->granule, o->seq++, p, n);
}
