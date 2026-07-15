#!/bin/bash
# Downloads the two typefaces (SIL Open Font License) and cuts the static
# instances the renderer expects. Requires: curl, python3 with fonttools.
set -euo pipefail
cd "$(dirname "$0")"

curl -sL -o Fraunces-VF.ttf "https://github.com/google/fonts/raw/main/ofl/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"
curl -sL -o Fraunces-Italic-VF.ttf "https://github.com/google/fonts/raw/main/ofl/fraunces/Fraunces-Italic%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"
curl -sL -o SpaceGrotesk-VF.ttf "https://github.com/google/fonts/raw/main/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf"

# Fraunces is a variable font with optical-size, weight, softness and wonk axes.
# The dashboard uses display cuts at opsz=144 for the big numerals and headings.
python3 -m fontTools.varLib.instancer Fraunces-VF.ttf opsz=144 wght=560 SOFT=0 WONK=1 -o Fraunces-Display.ttf
python3 -m fontTools.varLib.instancer Fraunces-Italic-VF.ttf opsz=144 wght=520 SOFT=0 WONK=1 -o Fraunces-DisplayItalic.ttf
python3 -m fontTools.varLib.instancer SpaceGrotesk-VF.ttf wght=400 -o SpaceGrotesk-Regular.ttf
python3 -m fontTools.varLib.instancer SpaceGrotesk-VF.ttf wght=500 -o SpaceGrotesk-Medium.ttf
python3 -m fontTools.varLib.instancer SpaceGrotesk-VF.ttf wght=700 -o SpaceGrotesk-Bold.ttf

rm -f Fraunces-VF.ttf Fraunces-Italic-VF.ttf SpaceGrotesk-VF.ttf
echo "Fonts ready."
