#!/usr/bin/perl

use strict;
use warnings;
use JSON::PP qw(decode_json);
use Encode qw(encode);
use LoxBerry::Log;

my ($filename, $package, $level) = @ARGV;
my %params = (
    name => 'FlowerCare', filename => $filename, package => $package,
    addtime => 1,
);
$params{loglevel} = $level if defined $level; # Optional command-line --debug override.
my $log = LoxBerry::Log->new(%params);
$| = 1;
while (my $line = <STDIN>) {
    my $record = decode_json($line);
    my $method = $record->{method};
    die "Invalid log method" unless $method =~ /\A(?:LOGSTART|LOGEND|INF|OK|DEB|WARN|ERR)\z/;
    # The native viewer treats message text as HTML; D-Bus reprs must not become tags.
    my $message = $record->{message};
    $message =~ tr/<>/()/;
    # LoxBerry::Log writes to a raw filehandle, so pass UTF-8 bytes, not characters.
    $log->$method(encode('UTF-8', $message));
    if ($method eq 'LOGSTART') { print "READY\n"; }
    last if $method eq 'LOGEND';
}
