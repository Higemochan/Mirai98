Mirai98
=======

A PC98 virtualization platform.

Boot a PC from a USB stick and it becomes a PC-98 hypervisor: create
virtual PC-9821s, run them, and drive their screens from a browser on
another machine.  Nothing is installed on the PC unless you ask for it.

Note:
- The compatible BIOS is becoming better, but not stable for now.
- Please use PC-9821 Xa7C9W ROMs for better Windows virtualization.

## How to use

- Write the USB image file to a USB stick.
    - Use `rufus.exe` or `BalenaEtcher`
- Insert the USB stick to your PC, then turn on the power.
- Enter the boot menu and select the USB stick.
- Mirai98 will show its IP address and a URL. (e.g. `http://192.168.1.10:8098`)
- Access the URL via a browser on another PC.

## First run

The browser asks three things, once:

- **Language**: English or Japanese.
- **Where the machines live**: the free space on the stick is the
  default and needs no thought.  Pick another drive and Mirai98 makes a
  `Mirai98` folder on it; only that choice is kept on the stick, so
  taking the drive away costs you the machines and nothing else.
- **A password**: leave it empty on a private network.  Set one and it
  becomes the root password, so the web console, the shell and ssh all
  ask for it.

A FreeDOS(98) machine is already there to try.

## What you get

- Create and edit virtual machines: PC-9821 or PC-9801, memory, disks,
  sound, network, real or compatible ROMs.
- The guest's screen in the browser, with its sound.
- Disk images: upload, download, convert (raw/qcow2/hdi/fdi), look
  inside a FAT image, write one to a real drive or read one off it.
- A shell on the host, and the system log.
- Install to an internal disk, or keep everything on the stick.

## Requirements

- An x86-64 PC that can boot from USB (BIOS or UEFI).
- 2 GB of RAM or more.  The system copies itself into RAM, so the stick
  can be pulled once it is up.
- Hardware virtualisation (VT-x/AMD-V) if you want the guests to be
  quick.  Without it they still run, only slower.

The data partition grows to fill the stick on the first boot, so a
larger stick simply gives you more room for disk images.

## Building it yourself

See [BUILD.md](BUILD.md).  One script builds the whole image; QEMU comes
from [qemu-pc98](https://github.com/awemorris/qemu-pc98).
