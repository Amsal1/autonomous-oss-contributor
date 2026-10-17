# docs

`env` in this directory is a symlink to `/proc/self/environ` in the built fixture. The snapshot
must turn it into `env.symlink.txt`, and execd must answer `ELINK` when asked to read it.
