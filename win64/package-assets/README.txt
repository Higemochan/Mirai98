QEMU-11.0 with PC-9821 patch

[Acknowledgments]

This program is an integration of the following emulators to qemu-11.0:

- qemu/9821 (qemu-0.10 fork)
- NP2
- NP21/W

Instead of directly integrating the source code, I analyzed how these
emulators work and reimplemented them to match QEMU's internal interface.

Since I only have several PC98 machines, it's difficult for me to pursue
accurate peripheral emulation. I believe the best approach is to use QEMU
for PC98 OS development, while relying on NP21/W for playing various
fun apps and games.

[Boot]

 A minimal compatible BIOS is included. (Replace with a real Xa7 BIOS + Sound ROM if you want.)

 FDD boot:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder -drive if=floppy,format=raw,file=dos62-fdd.raw

 2-FDD:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder \
     -drive if=floppy,unit=0,format=raw,file=system-fdd.raw \
     -drive if=floppy,unit=1,format=raw,file=data-fdd.raw

 HDD boot:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder -drive \
     if=ide,bus=0,unit=0,format=qcow2,file=dos62-hdd.qcow2

 HDD boot with 86 sound:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder \
       -drive if=ide,bus=0,unit=0,format=qcow2,file=dos620-hdd.qcow2 \
       -audiodev dsound,id=snd \
       -device pc98-opna,audiodev=snd

 HDD boot with WSS sound:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder \
       -drive if=ide,bus=0,unit=0,format=qcow2,file=dos620-hdd.qcow2 \
       -audiodev dsound,id=snd \
       -device pc98-wss,audiodev=snd

 HDD boot with CD-ROM:
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder \
     -drive if=ide,bus=0,unit=0,format=qcow2,file=dos620-hdd40.qcow2 \
     -drive if=ide,bus=1,unit=0,media=cdrom,readonly=on,file=cd-image.iso

 Mounting the host file system: (MS-DOS is bootable!)
   qemu-system-x86_64 -M pc9821 -m 64M -L ./rom-folder \
     -drive if=ide,bus=0,unit=0,format=qcow2,file=dos620-hdd.qcow2 \
     -drive if=ide,bus=0,unit=1,format=raw,file=fat98:rw:my-folder-path

[Hardware]

Base: (-M pc9801)
- Floppy (1.2MB and 1.4MB)
- IDE HDD and CD-ROM
- Text (80x25) and graphic display (640x400 16-color)
- Keyboard
- Mouse
- Serial port

9821-only: (-M pc9821)
- PCI-Bus
- Window Accelerator Board (Xe10, Core-Graph and Cirrus Logic GD5440)
- USB 1.1/2.0 Host Controller

Optional: (for both pc9801 and pc9821)
- SCSI HDD and CD-ROM (not stable yet)
- Sound (PC-9801-86 and Mate-X PCM)
- Ethernet (LGY-98)


[Software]

- MS-DOS 6.2
- Windows 95
- Windows 2000
- FreeBSD 8.4-RELEASE
- Plamo Linux

[IRQ]

0	Timer
1	Keyboad
2	VSync
3	WSS
4	Serial
5	SCSI
6	LAN
7	PIC Cascade
8	x87 FERR
9	IDE
10	FDC 640KB
11	FDC 1MB
12	OPNA
13	Bus Mouse
14	PCI INT
15	RTC

[DMA]

0	SCSI
1	WSS
2	FDC 1MB
3	FDC 640KB

[Note]

KVM and Hyper-V supports are not stable yet.
