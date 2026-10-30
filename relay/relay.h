/* The relay: dials the bridge over TLS and plays its Opus through the stock ffmpeg into aplay.sh
 * (docs/wire.md). One process, one poll loop. */
#ifndef RELAY_H
#define RELAY_H
#include "sys.h"

#define FRAME_MS 20
#define MAX_ENDPOINTS 8

extern int verbose;
extern volatile int quitting, retry_remote;
int relay_send(char type, const void *payload, int n);
void say(const char *fmt, ...);
uint64_t now_ms(void);
/* Waits for events on fd until deadline (ms, now_ms's clock), feeding the sink meanwhile: the
 * revents, 0 at the deadline. fd < 0 just waits. */
int relay_wait(int fd, short events, uint64_t deadline);

/* remote.c: .soloify/remote, one endpoint a line: HOST PORT TOKEN [sha256=HEX] [buffer=MS] */
struct endpoint {
    char host[128], port[8], token[80];
    unsigned char pin[32];
    int pinned, lan, buffer_ms;
};
int remote_load(const char *path, struct endpoint *eps, int max);

/* tls.c */
struct link { int fd; SSL_CTX *ctx; SSL *ssl; };
int link_open(struct link *l, const struct endpoint *ep);
int link_read(struct link *l, void *buf, int n);        /* >0 bytes, 0 none yet, -1 gone */
int link_write(struct link *l, const void *buf, int n); /* bytes taken (maybe 0), -1 gone */
void link_close(struct link *l);

/* wire.c: frames of a type byte and a big-endian u32 length */
struct wire {
    unsigned char in[(1 << 20) + 5], out[32768];
    int nin, nout;
};
int wire_send(struct wire *w, char type, const void *payload, int n); /* -1: no room */
/* The next whole frame in w->in: its type, with *payload and *n set; 0 when none yet, -1 if too
 * big. wire_consume drops it once handled. */
int wire_next(struct wire *w, const unsigned char **payload, int *n);
void wire_consume(struct wire *w);
uint32_t be32(const unsigned char *p);
void put_be32(unsigned char *p, uint32_t v);

/* ogg.c: one page per packet; out needs 27 + 6 + n bytes for a packet, 128 for the headers */
struct ogg { uint32_t seq; uint64_t granule; };
void ogg_init(void);
int ogg_headers(struct ogg *o, unsigned char *out, unsigned skip);
int ogg_packet(struct ogg *o, unsigned char *out, const unsigned char *p, int n, unsigned samples, int end);

/* sink.c: the decoder and aplay.sh; sink_start gives the pipe to write the Ogg into */
int sink_start(void);
int sink_rendered(uint64_t *frames, unsigned *rate);
int sink_next(int fd); /* drain/reap only the decoder, retaining the PCM player */
void sink_finish(int fd);
int sink_player(void);
void sink_stop(void);
int sink_exited(void);

/* q2pod.c: -q, librespot's state, controls and the minimal local browser */
void q2pod_start(void);
void q2pod_tick(void);
void q2pod_stop(void);
void q2pod_config(int valid);
void q2pod_link(int up);
void q2pod_frame(int type, const unsigned char *p, int n);
void q2pod_started(void); /* the first buffered packet is about to reach the decoder */
void q2pod_paused(void); /* stop/reap before publishing the frozen bookmark */
void q2pod_capture(void); /* a fresh encoder begins after ordered control boundaries */
int q2pod_wants(void);
int q2pod_audio(void);
int q2pod_ready(void);

/* audio.c: the ring, the Ogg stream and the sink */
void audio_start(int target_ms); /* a link is up, with this buffer target */
void audio_packet(const unsigned char *p, int n, unsigned seq);
int audio_pollfd(struct pollfd *pfd); /* 1 with *pfd set when the sink wants waiting on */
void audio_service(short revents);
void audio_tick(void);
void audio_enable(int enabled); /* stop/reap and clear, or enable fresh capture */
void audio_pause(void);
int audio_bookmark(uint64_t *sample); /* hardware presentation sample clock, in ms */
void audio_capture(unsigned generation, unsigned skip, const unsigned char *primer, int n);
void audio_end(unsigned seq, unsigned samples);
void audio_clear(void); /* a seek/skip: stop and discard everything */
uint64_t audio_received(void);
uint64_t audio_played(void);
int audio_target(void);
int audio_stats(char *buf, int cap);
#endif
