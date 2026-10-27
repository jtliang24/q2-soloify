/* Until the ring and the sink: counts what arrives. */
#include "relay.h"

static unsigned packets, bytes;

void audio_start(int target_ms) {}
void audio_packet(const unsigned char *p, int n) { ++packets, bytes += n; }
int audio_pollfd(struct pollfd *pfd) { return 0; }
void audio_service(short revents) {}
void audio_tick(void) {}
int audio_stats(char *buf, int cap) { return snprintf(buf, cap, "packets=%u bytes=%u", packets, bytes); }
