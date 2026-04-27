# Maintainer: Bill Sideris <bill88t@feline.gr>

pkgname=beryllium-tools
pkgver=1.14.0
pkgrel=1
pkgdesc="A grand collection of tools"

arch=('any')
url="https://github.com/BredOS/bredos-tools"
license=('GPL3')

groups=(bredos)
depends=('python' 'arch-install-scripts' 'systemd' 'gcc')
provides=('bredos-tools')
replaces=('bredos-tools')
conflicts=('bredos-tools')

optdepends=(
    'dtc: Compile device trees with the dtsc helper'
    'android-tools: Use rv2rk and rvflasher scripts for flashing Ky RISC-V chips'
)

source=('dtsc.py'
        'rkdump.sh'
        'beryllium-chroot.sh'
        'lsmmc.py'
        'wakeupctl.py'
        'grub-password.sh'
        'grub-apply-unrestrict.py'
        'grub-unrestrict.hook'
        'sleepctl.py'
        'sleepctld.sh'
        'sleepctl.service'
        'rv2rk.py'
        'FSBL.bin'
        'u-boot.itb'
        'rvflasher.py'
        'FSBL_FLASH.bin'
        'u-boot_FLASH.itb'
        'partition_universal.json'
        'bootinfo_sd.bin'
        'rkdump.1'
        'grub-password.1'
        'dtsc.1'
        'sleepctl.1'
        'sleepctld.1'
        'wakeupctl.1'
        'beryllium-chroot.8')
sha256sums=('f264899c639e3e8897e2daaef00a035a85ab51e39ba9ae5bb32d31e41d5394eb'
            '0105d6b791bdc4289c2cd3227fd4a836182a8f14261751a70b636606054452ae'
            '3f8adbb46b4d0345ad558393ab66b4fa50d33c5761b742a513cf7aff803a94ee'
            '5b8dd87f4141cdce1aa73eb19fca30370c4ab5fc1f97f8077cd01bf68a51a4cc'
            '12047c25a46a9e0def5cc687ed0d1690d8e80853680280d347b1102b5203bde3'
            'be81b089e5bb91a9a3c2ae6c6658d538ea2b031263e3ac9685be2c1ec87fba6f'
            'f430e73417126b2dcf84cfaa02b3fb5c520da5794faf8d29f9c8531ec970614e'
            'ffabbfbfdca391f8616340a4323eddb868040ca35c24bd8d7d6c5df3b2cc77ac'
            '797103948bd377f8aae15527eafcdb7e4d9121d4ce57ac7af0943bae26dbdca5'
            '3c830af040906c5de9e4f82f7089775b585cd46eafc435053bef1aee717f8d49'
            '7bde0bb9eb48c7c560194d04a0864c833e63662d3ff527a23844eaa0d1849101'
            'adbcefef20db5743a34b75f463c73aefcfb86b16badc33f0edb30b947cb7ac0e'
            '93a0b2e1b8181818ade215f00937db2decea79639147dcad2eb9faa8d669f4e4'
            'fb163aa1ba382e2a6009c8e1b468494b5ab11aa80c500012590226f1fb554040'
            '8342ff36a1caf3062424bb362a0bee89ab72be90729a2c2690ff62509ac94209'
            'b4113d322513cdc13a487233b77cc5a7d487e551f8db4496056a3c1dd41659f5'
            '9484899dc57aacacae666a7b66416fe4d424372bb6a9540677cd596a21edc1fe'
            '5ee0f6f807e3b83f6a43826c9130b001232ab8d42eb664e4087daeca3853ac98'
            'f339e3e576ce94d7812c2887622c5882ae12ac3c9a98059aef37741850a43cb6'
            'ccaab9ca8f25571d5809b82f7be9a7133d91a75c745ff7174d5c78c593510659'
            '99646c23b88b74fa6fa9220588cb7cc18b1782fa8642559ce237adfc8b98ef01'
            '9a3d90776fb514bbcbfdb8cef0034555c093906eeac6509d3faa460fe04d7371'
            'b0543503053367280b216f534941b39460a04461a5d19834ab679677275761c6'
            'bb1ff999bca9352af32caca4e92cfd5516954957dd9d4397b3f4bbcde26f8302'
            '13b871e82b556190e0f221caeb5b719b48355e908eacba1ab6ea863fb5e658e4'
            'ef25ee68d18f85fb9a9b8a1976fcdd55828fdc968a94cdeea78490c44680f6f6')

package() {
    # DTSC
    install -Dm755 "$srcdir/dtsc.py" "$pkgdir/usr/bin/dtsc"

    # RKDUMP
    install -Dm755 "$srcdir/rkdump.sh" "$pkgdir/usr/bin/rkdump"

    # Wakeupctl
    install -Dm755 "$srcdir/wakeupctl.py" "$pkgdir/usr/bin/wakeupctl"

    # BredOS-Chroot
    install -Dm755 "$srcdir/beryllium-chroot.sh" "$pkgdir/usr/bin/beryllium-chroot"
    ln -s "/usr/bin/beryllium-chroot" "$pkgdir/usr/bin/beryl-chroot"

    # lsmmc
    install -Dm755 "$srcdir/lsmmc.py" "$pkgdir/usr/bin/lsmmc"

    # GRUB Password
    install -Dm755 "$srcdir/grub-password.sh" "$pkgdir/usr/bin/grub-password"
    install -Dm755 "$srcdir/grub-apply-unrestrict.py" "$pkgdir/usr/bin/grub-apply-unrestrict"
    install -Dm644 "$srcdir/grub-unrestrict.hook" "$pkgdir/usr/share/libalpm/hooks/grub-unrestrict.hook"

    # Sleepctl
    install -Dm755 "$srcdir/sleepctl.py" "$pkgdir/usr/bin/sleepctl"
    install -Dm755 "$srcdir/sleepctld.sh" "$pkgdir/usr/bin/sleepctld"
    install -Dm644 "$srcdir/sleepctl.service" "$pkgdir/usr/lib/systemd/user/sleepctl.service"

    # rv2rk
    install -Dm755 "$srcdir/rv2rk.py" "$pkgdir/usr/bin/rv2rk"
    mkdir "$pkgdir/usr/share/rv2rk"
    install -Dm644 "$srcdir/FSBL.bin" "$pkgdir/usr/share/rv2rk/FSBL.bin"
    install -Dm644 "$srcdir/u-boot.itb" "$pkgdir/usr/share/rv2rk/u-boot.itb"

    # rvflasher
    install -Dm755 "$srcdir/rvflasher.py" "$pkgdir/usr/bin/rvflasher"
    mkdir "$pkgdir/usr/share/rvflasher"
    install -Dm644 "$srcdir/FSBL_FLASH.bin" "$pkgdir/usr/share/rvflasher/FSBL.bin"
    install -Dm644 "$srcdir/u-boot_FLASH.itb" "$pkgdir/usr/share/rvflasher/u-boot.itb"
    install -Dm644 "$srcdir/partition_universal.json" "$pkgdir/usr/share/rvflasher/partition_universal.json"
    install -Dm644 "$srcdir/bootinfo_sd.bin" "$pkgdir/usr/share/rvflasher/bootinfo_sd.bin"

    # Manual pages
    install -Dm644 "$srcdir/rkdump.1" "$pkgdir/usr/share/man/man1/rkdump.1"
    install -Dm644 "$srcdir/grub-password.1" "$pkgdir/usr/share/man/man1/grub-password.1"
    install -Dm644 "$srcdir/dtsc.1" "$pkgdir/usr/share/man/man1/dtsc.1"
    install -Dm644 "$srcdir/sleepctl.1" "$pkgdir/usr/share/man/man1/sleepctl.1"
    install -Dm644 "$srcdir/sleepctld.1" "$pkgdir/usr/share/man/man1/sleepctld.1"
    install -Dm644 "$srcdir/wakeupctl.1" "$pkgdir/usr/share/man/man1/wakeupctl.1"
    install -Dm644 "$srcdir/beryllium-chroot.8" "$pkgdir/usr/share/man/man8/beryllium-chroot.8"
}
