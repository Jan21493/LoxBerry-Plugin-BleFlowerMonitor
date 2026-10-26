#!/usr/bin/perl

use strict;
use warnings;
use JSON::PP qw(decode_json);
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
    $log->$method($record->{message});
    if ($method eq 'LOGSTART') { print "READY\n"; }
    last if $method eq 'LOGEND';
}
