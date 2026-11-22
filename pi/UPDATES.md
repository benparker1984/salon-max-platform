# Native till releases

The owner platform's **Updates** page reports each managed Pi's installed and
available version, last check, last successful install and any error. **Update
now** requests the approved stable release and retries a failed installation.
The updater checks every minute. A blank target follows stable. A pinned target
must name a published signed release in the selected channel.

## Paired Pi installation

Copy this `pi` directory to the Pi, including `releases` and
`release-public-key.hex`. Run as the desktop user, with the existing till PID.

```sh
python3 pi/install_managed_updates.py --existing-pid 1234
```

The service waits for that existing app to close before taking over. Its unit is
`systemctl --user status salonmax-native-till.service`. Native app output goes to
`~/.local/share/salonmax-updater/till.log`. The private state directory also holds
startup heartbeats, rollback state, previous releases and SQLite backups.
Use the installer's generated desktop environment values for this Pi.

## Publishing

Edit the native source under `pi/till-source`. Build a new, unique version using
the existing Ed25519 signing key, stored outside this repository.

```sh
python pi/build_release.py 2026.11.22.3 --key /private/location/release-signing-key.bin
```

Commit the source, signed manifest, ZIP and stable pointer together, push, and
verify the hosted health check and authenticated manifest feed. Test on the
desk Pi before promoting a release to salon tills. Keep the private signing key
backed up securely. Replacing it requires installing the new public key on each
Pi. Changing source without building and publishing a release does not update
tills.

## Restart and recovery

The signed manifest authenticates the archive checksum and schema compatibility.
Only native Python files are accepted. These releases use the existing database
schema and installed Python dependencies. Schema or dependency upgrades require
a separately tested migration before publishing a compatible native release.

The Qt thread only accepts a restart while signed out, with idle beds and empty
queues and baskets. A new sign-in or active queue postpones it. Before switching,
the supervisor takes a consistent SQLite backup. A new process must publish fresh
GUI heartbeats for 20 seconds within the startup timeout. A failed startup restores
the previous release and records the failure. An interrupted installation also
selects the previous release on service startup. The database backup is kept for
recovery and is not automatically restored over subsequent business transactions.

The initial installation is complete only once the existing unmanaged till has
closed and the new service has reported its first successful startup.
