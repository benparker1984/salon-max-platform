# Ground Hog — full Pi SD-card emergency erasure

**Status: staged, NOT enabled on the test till. No SD card has been erased.**
The native menu entry is visible only to an active owner. Managers/staff cannot
open it. A harmless name is not a security control: authorization is checked
again in the privileged controller, not just by hiding a button.

Once separately installed, tested and enabled, the action requires:

1. Current owner sign-in, owner PIN confirmation, an active manager or owner PIN,
   typing `ERASE SD CARD`, and a final irreversible-action confirmation.
2. No active bed, queue or sale basket, with a fresh managed-till heartbeat.
3. A PC-signed successful backup receipt less than 24 hours old, bound to this Pi
   serial and SD-card CID. The business-data fingerprint (accounts/minutes,
   transactions, courses, stock, staff, beds, pricing, settings and cash sessions)
   must still match. If it changed, run a fresh PC backup before attempting erasure.
4. A root-installed, root-owned helper and pinned PC public signing key.

PIN failures are limited to five per 15 minutes. PINs are passed through stdin,
not process arguments or logs. The PC private signing key stays on the PC. The
normal till application has no raw block-device write fallback.

The root controller rechecks the backup, stops the native till/back-office/sync
services, and exclusively checks the database before configuring a one-time
recovery boot. Failure restores boot configuration and resumes services. A
separate transient root unit handles the restart, outside the GUI service group.

The recovery image runs entirely in RAM. It does not mount the SD filesystem. It
verifies the physical Pi serial, SD CID, capacity and one-time boot nonce again,
then overwrites the entire **/dev/mmcblk0** addressable capacity, flushes writes,
checks both ends and powers off. It never selects a USB disk or another target.
The operating system, local databases, credentials, logs and local backups go
with the card. The encrypted PC backup is not reachable by this erase program.

This is a logical full-card overwrite, **not certified physical sanitisation** of
wear-levelled flash. A compromised OS/root user can interfere with local controls.
Emergency incident response may also require revoking device/account credentials
and investigating the cloud; wiping a card does not secure other systems.
Erasing this 128 GB card may take hours. Keep power connected. A failed or
interrupted write is not certified erased and may leave the card unbootable.

## Required commissioning before activation

The Codex session has no unattended sudo permission. It cannot install a root
helper or change the Pi's boot configuration. Therefore the GUI fails closed with
“not installed”; it must not claim the emergency wipe is operational.

First verify `~/SalonMaxBackups/receipt-signing-public.xml` on the PC matches
`~/.local/share/salonmax-recovery/pc-signing-public.xml` on the Pi. The initial key
pinning must happen before trusting a potentially compromised Pi.

On a cloned, expendable SD card on the physical Pi, with an independently restored
PC backup, stage the installer:

```sh
sudo python3 ~/Salon-Max/ops/groundhog/install.py
```

Staging is disabled. It does not change boot files, restart or erase anything.
Review the code and validate console/display, kernel/BusyBox dynamic libraries,
SD drivers, bound hardware, failure recovery and full-card erase on the spare
card. That destructive physical test has **not** been performed by this session.
The installer explicitly enables only when run with
`--enable-after-spare-card-test`. It must bind the CID of the actual card after
commissioning; a replacement card never inherits permission from a different CID.

Do not enable on a production card until the physical test and backup recovery
are complete. Back up the current original boot files before that test. Recover
from a failed pre-erase boot by mounting the card on another computer and restoring
`original-config.txt` and `original-cmdline.txt` from the root helper state.

## Automated checks

Authorization, hardware-idle guards, receipt signatures/age/device binding/account
fingerprint, PIN confirmation and RAM archive packaging can be tested without
erasing a device. They do not replace the required physical spare-card boot test.
