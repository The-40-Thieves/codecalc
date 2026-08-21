/* blocknet.c — preloaded shim that blocks network egress for sandboxed code.
 *
 * Fails socket() and connect() with EACCES for NETWORK address families only
 * — AF_INET and AF_INET6 — for any call that resolves socket()/connect()
 * through the symbol this shim replaces. That is not every way to reach the
 * network; see "Best-effort by construction" below for the two classes of
 * call that never touch this symbol at all.
 *
 * It used to block socket() unconditionally, which also killed AF_UNIX. A unix
 * socket is local IPC and cannot leave the machine, so blocking it bought no
 * security and broke things that had nothing to do with egress: multiprocessing,
 * runtime event loops, and anything talking to a local socket file. Verified —
 * with the old shim, AF_UNIX returned EACCES. That is the same mistake as
 * blocking socketpair(), which the next paragraph already knew not to make.
 *
 * Deliberately does NOT touch socketpair(): runtimes (tokio, libuv, glibc) use
 * it for internal signal and event plumbing, and blocking it crashes the
 * runtime rather than its network call.
 *
 * Best-effort by construction, in two independent ways. It only affects
 * dynamically-linked programs: a statically linked binary — Go's default —
 * never consults the dynamic loader and ignores this entirely. And even a
 * dynamically-linked program can route AROUND the interposed symbol rather
 * than through it: `ctypes`/`dlsym` pulling `socket()` straight out of libc,
 * or a raw `syscall(SYS_socket, ...)`, never resolves the name this shim
 * replaces, so neither is seen here at all. Verified live: with this shim
 * loaded and `no_net=True`, `ctypes.CDLL(find_library("c")).socket(2, 1, 0)`
 * returns a working fd (E-1) — the executor's `unenforced`
 * now discloses this whenever the shim is what satisfied `no_net`, rather
 * than reporting it as fully applied. Containers or a real kernel-level
 * egress block (gVisor, network namespaces) are the fix for either gap; this
 * shim is a speed bump against the ordinary case, not a boundary against an
 * adversarial one.
 *
 * Build:
 * Built automatically by executor/build.rs as part of `cargo build`, into the
 * same directory as the executable (which is where it is looked up at run
 * time). Do NOT build it by hand: a shim that lags this source enforces the
 * OLD policy while every "is the shim present?" check still passes, which is
 * how an AF_UNIX fix here stayed inert for an entire test run.
 *
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
 * Two macOS caveats remain regardless of the mechanism, and both make --no-net
 * weaker there than on Linux:
 *
 *   1. SIP and the hardened runtime strip DYLD_INSERT_LIBRARIES for protected
 *      and hardened-signed binaries, which includes most signed interpreters.
 *      AMFI can refuse interposing outright.
 *   2. Interposing applies between the process and the images dyld loads. It
 *      does NOT reach calls made INSIDE the dyld shared cache, where libSystem
 *      lives — so a program's own socket()/connect() is intercepted, but a
 *      system framework that opens a connection internally is not.
 *
 * The executor reports --no-net as unenforced when no shim is applied, but it
 * cannot detect either of these at runtime. Treat macOS --no-net as a
 * best-effort speed bump, never as isolation. Containers are the answer for
 * anything stronger, on every platform.
 *
 * On Linux this shim is no longer the enforcement mechanism: the executor
 * installs a seccomp-bpf filter in the child (see executor/src/platform/unix.rs)
 * that refuses the socket(AF_INET/AF_INET6) SYSCALL in-kernel, closing the
 * ctypes/dlsym/raw-syscall bypass described above. This shim remains the
 * no_net path on macOS, and the Linux fallback when the kernel refuses a
 * seccomp filter — everything above about it being best-effort is still true
 * for those two cases.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/types.h>

#if !defined(__APPLE__)
#include <dlfcn.h>
#endif

/* Only these can reach a network. AF_UNIX/AF_NETLINK/AF_ALG are local. */
static int blocknet_is_network(int domain)
{
    return domain == AF_INET || domain == AF_INET6;
}

static int blocknet_addr_is_network(const struct sockaddr *addr)
{
    if (addr == NULL) {
        return 0;
    }
    return blocknet_is_network((int)addr->sa_family);
}

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

/* On macOS a call made from inside this library is not itself interposed, so
 * the real implementation is reachable by name. */
__attribute__((used)) static int blocknet_socket(int domain, int type, int protocol)
{
    if (!blocknet_is_network(domain)) {
        return socket(domain, type, protocol);
    }
    errno = EACCES;
    return -1;
}

__attribute__((used)) static int blocknet_connect(int fd, const struct sockaddr *addr,
                                                  socklen_t len)
{
    if (!blocknet_addr_is_network(addr)) {
        return connect(fd, addr, len);
    }
    errno = EACCES;
    return -1;
}

DYLD_INTERPOSE(blocknet_socket, socket);
DYLD_INTERPOSE(blocknet_connect, connect);

#else /* Linux and other ELF platforms: definition order is enough */

/* ELF: our definition shadows libc's, so reaching the real one needs
 * dlsym(RTLD_NEXT). Calling socket() here would recurse into ourselves. */
int socket(int domain, int type, int protocol)
{
    if (!blocknet_is_network(domain)) {
        static int (*real_socket)(int, int, int);
        if (real_socket == NULL) {
            real_socket = (int (*)(int, int, int))dlsym(RTLD_NEXT, "socket");
        }
        if (real_socket != NULL) {
            return real_socket(domain, type, protocol);
        }
    }
    errno = EACCES;
    return -1;
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    if (!blocknet_addr_is_network(addr)) {
        static int (*real_connect)(int, const struct sockaddr *, socklen_t);
        if (real_connect == NULL) {
            real_connect = (int (*)(int, const struct sockaddr *, socklen_t))
                dlsym(RTLD_NEXT, "connect");
        }
        if (real_connect != NULL) {
            return real_connect(fd, addr, len);
        }
    }
    errno = EACCES;
    return -1;
}

#endif
