# SysWatch Concepts

SysWatch has one background sampler thread. It reads host telemetry through psutil and publishes a locked snapshot. HTTP handlers read that snapshot instead of sampling the machine themselves.

## Process states
The UI normalizes common psutil states to R/S/D/T/Z/I. Platform-specific states can appear as ?.

## Process controls
POSIX systems use terminate/kill and SIGSTOP/SIGCONT. Windows uses psutil process termination and suspend/resume APIs. Protected processes can return permission errors.

## Priority
Unix-like systems expose numeric nice values, normally -20 through 19. Windows exposes priority classes; SysWatch translates numeric requests to the nearest class.

## Memory
System memory is total/used/available physical memory. Process memory is RSS, the resident set currently held in physical memory.

## Network and disk rates
Interface and disk I/O counters are sampled once per second and represented as deltas from the previous sample.

## Cross-platform data path
- Linux: psutil uses kernel interfaces including /proc and system calls.
- macOS: psutil uses Darwin facilities such as sysctl/libproc.
- Windows: psutil uses Win32/NT APIs and performance counters.

SysWatch does not shell out to ps, top, tasklist, or similar utilities.
