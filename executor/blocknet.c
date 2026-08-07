/* blocknet.c — preloaded shim that blocks network egress for sandboxed code.
 *
 * Fails socket() and connect() with EACCES. Deliberately does NOT touch
 * socketpair(): runtimes (tokio, libuv, glibc) use it for internal signal and
 * event plumbing, and blocking it crashes the runtime rather than its network
 * call.
 *
 * Best-effort by construction: it only affects dynamically-linked programs.
 * A statically linked binary — Go's default — never consults the dynamic
 * loader and ignores this entirely.
 *
 * Build:
 *   Linux  cc -shared -fPIC -O2 -o blocknet.so    blocknet.c   (LD_PRELOAD)
 *   macOS  cc -shared -fPIC -O2 -o blocknet.dylib blocknet.c   (DYLD_INSERT_LIBRARIES)
 *
 * The two platforms need DIFFERENT mechanisms, and this is not a detail that
 * can be skipped. Linux resolves symbols in load order, so simply DEFINING
 * socket() in a preloaded object shadows libc's. macOS uses a two-level
 * namespace: each call site is bound to the specific library it was linked
 * against, so an identically-named function in an inserted dylib is never
 * consulted. The supported mechanism there is a __DATA,__interpose section,
 * which dyld reads and applies to every image it loads.
 *
 * That difference was found by CI, not by reading: the macOS job built the
 * dylib, preloaded it, and watched the probe connect anyway.
 *
 * macOS caveat that remains regardless: SIP strips DYLD_INSERT_LIBRARIES for
 * protected and hardened-runtime binaries, which includes most signed
 * interpreters. `--no-net` is weaker on macOS than on Linux and the executor
 * reports it as such.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/types.h>

#if defined(__APPLE__)

/* dyld applies each {replacement, replacee} pair it finds in this section. */
#define DYLD_INTERPOSE(_replacement, _replacee)                                \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(unsigned long)&_replacement,                            \
        (const void *)(unsigned long)&_replacee                                \
    }

__attribute__((used)) static int blocknet_socket(int domain, int type, int protocol)
{
    (void)domain;
    (void)type;
    (void)protocol;
    errno = EACCES;
    return -1;
}

__attribute__((used)) static int blocknet_connect(int fd, const struct sockaddr *addr,
                                                  socklen_t len)
{
    (void)fd;
    (void)addr;
    (void)len;
    errno = EACCES;
    return -1;
}

DYLD_INTERPOSE(blocknet_socket, socket);
DYLD_INTERPOSE(blocknet_connect, connect);

#else /* Linux and other ELF platforms: definition order is enough */

int socket(int domain, int type, int protocol)
{
    (void)domain;
    (void)type;
    (void)protocol;
    errno = EACCES;
    return -1;
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    (void)fd;
    (void)addr;
    (void)len;
    errno = EACCES;
    return -1;
}

#endif
