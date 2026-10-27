#!/bin/sh

# Bash script which is executed in case of an update (if this plugin is already
# installed on the system). This script is executed as very last step (*AFTER*
# postinstall) and can be for example used to save back or convert saved
# userfiles from /tmp back to the system. Use with caution and remember, that
# all systems may be different!
#
# Exit code must be 0 if executed successfull. 
# Exit code 1 gives a warning but continues installation.
# Exit code 2 cancels installation.
#
# Will be executed as user "loxberry".
#
# You can use all vars from /etc/environment in this script.
#
# We add 5 additional arguments when executing this script:
# command <TEMPFOLDER> <NAME> <FOLDER> <VERSION> <BASEFOLDER>
#
# For logging, print to STDOUT. You can use the following tags for showing
# different colorized information during plugin installation:
#
# <OK> This was ok!"
# <INFO> This is just for your information."
# <WARNING> This is a warning!"
# <ERROR> This is an error!"
# <FAIL> This is a fail!"

# To use important variables from command line use the following code:
COMMAND=$0    # Zero argument is shell command
PTEMPDIR=$1   # First argument is temp folder during install
PSHNAME=$2    # Second argument is Plugin-Name for scipts etc.
PDIR=$3       # Third argument is Plugin installation folder
PVERSION=$4   # Forth argument is Plugin version
#LBHOMEDIR=$5 # Comes from /etc/environment now. Fifth argument is
              # Base folder of LoxBerry
PTEMPPATH=$6  # Sixth argument is full temp path during install (see also $1)

# Combine them with /etc/environment
PCGI=$LBPCGI/$PDIR
PHTML=$LBPHTML/$PDIR
PTEMPL=$LBPTEMPL/$PDIR
PDATA=$LBPDATA/$PDIR
PLOG=$LBPLOG/$PDIR # Note! This is stored on a Ramdisk now!
PCONFIG=$LBPCONFIG/$PDIR
PSBIN=$LBPSBIN/$PDIR
PBIN=$LBPBIN/$PDIR

echo -n "<INFO> Current working folder is: "
pwd
echo "<INFO> Command is: $COMMAND"
echo "<INFO> Temporary folder is: $PTEMPDIR"
echo "<INFO> (Short) Name is: $PSHNAME"
echo "<INFO> Installation folder is: $PDIR"
echo "<INFO> Plugin version is: $PVERSION"
echo "<INFO> Plugin CGI folder is: $PCGI"
echo "<INFO> Plugin HTML folder is: $PHTML"
echo "<INFO> Plugin Template folder is: $PTEMPL"
echo "<INFO> Plugin Data folder is: $PDATA"
echo "<INFO> Plugin Log folder (on RAMDISK!) is: $PLOG"
echo "<INFO> Plugin CONFIG folder is: $PCONFIG"

echo "<INFO> Copy back existing config files"
if ! cp -v -r "/tmp/uploads/$PTEMPDIR/_upgrade/config/." "$PCONFIG/"; then
    echo "<FAIL> Cannot restore the existing Flower Monitor configuration."
    exit 2
fi

echo "<INFO> Adding new config parameters"
# Preserve existing transport choices, including UDP-only installations.
for entry in \
    "BTADAPTER=default" "UDPENABLED=1" "MQTTENABLED=0" "MQTTTOPIC=miflora"
do
    key=${entry%%=*}
    if ! grep -q -E "^[[:space:]]*$key[[:space:]]*=" "$PCONFIG/miflora.cfg"; then
        printf '\n%s\n' "$entry" >> "$PCONFIG/miflora.cfg" || exit 2
    fi
done

MIGRATION_STATUS=0
if ! /usr/bin/python3 "$PBIN/miflora.py" --migrate-config --configfile "$PCONFIG/miflora.cfg"; then
    echo "<WARNING> Please select Default, USB or Onboard in the Flower Monitor settings and save before polling."
    MIGRATION_STATUS=1
fi

echo "<INFO> Copy back existing log files"
cp -v -r /tmp/uploads/$PTEMPDIR/_upgrade/log/* $PLOG/

echo "<INFO> Remove temporary folders"
#rm -r /tmp/uploads/$PTEMPDIR/_upgrade


# Replace real subfolder and scriptname
#/bin/sed -i "s#PLUGINDATAFOLDER#$PDATA#" $LBHOMEDIR/bin/plugins/$PDIR/miflora.py
#/bin/sed -i "s#PLUGINCONFIGFOLDER#$PCONFIG#" $LBHOMEDIR/bin/plugins/$PDIR/miflora.py
#/bin/sed -i "s#SYSTEMCONFIGFOLDER#$LBSCONFIG#" $LBHOMEDIR/bin/plugins/$PDIR/miflora.py

/bin/sed -i "s#PLUGINFOLDER#$PDIR#g" $LBHOMEDIR/system/cron/cron.hourly/$PSHNAME

exit "$MIGRATION_STATUS"
