#!/usr/bin/perl

use strict;
use warnings;
use CGI;
use Config::Simple;
use HTML::Template;
use JSON::PP ();
use POSIX qw(setsid);
use LoxBerry::System;
use LoxBerry::Web;

my $cgi = CGI->new;
LoxBerry::Web::lbheader("Xiaomi MiFlora", "https://github.com/Jan21493/Loxberry-Plugin-MiFlora", "help.html");
my $pcfg = Config::Simple->new("$lbpconfigdir/miflora.cfg")
    or die Config::Simple->error();
my $template = HTML::Template->new(
    filename => "$lbptemplatedir/index.html",
    global_vars => 1,
    loop_context_vars => 1,
    die_on_bad_params => 0,
);
my %L = LoxBerry::System::readlanguage($template, "language.ini");
foreach my $key (keys %L) {
    if ($key =~ /^MIFLORA\.(.+)$/) {
        $template->param($1 => $L{$key});
    }
}
my %miniservers = LoxBerry::System::get_miniservers();
my @errors;
my $message = "";
my %fields = (
    chkPluginEnabled => ['ENABLED', '1'],
    selPollFrequency => ['POLLFREQUENCY', '12'],
    chkLocaltime => ['LOCALTIME', '1'],
    selBTAdapter => ['BTADAPTER', 'serial'],
    chkMQTTEnabled => ['MQTTENABLED', '1'],
    txtMQTTTopic => ['MQTTTOPIC', 'miflora'],
    chkUDPEnabled => ['UDPENABLED', '0'],
    selMiniServer => ['MINISERVER', 'MINISERVER1'],
    txtMsUDPPort => ['UDPPORT', '5006'],
);
my %values;
foreach my $field (keys %fields) {
    my ($key, $default) = @{$fields{$field}};
    my $saved = $pcfg->param("MIFLORA.$key");
    $saved = join(',', @$saved) if ref($saved) eq 'ARRAY';
    $values{$field} = defined $saved ? $saved : $default;
}

my @adapters;
if (open(my $inventory, '-|', '/usr/bin/python3', "$lbpbindir/miflora.py", '--list-adapters')) {
    my $json = do { local $/; <$inventory> };
    if (close($inventory)) {
        my $decoded = eval { JSON::PP::decode_json($json) };
        if ($@ || ref($decoded) ne 'ARRAY') {
            push @errors, $L{'MIFLORA.errAdapterInventory'};
        } else {
            @adapters = @$decoded;
        }
    } else {
        push @errors, $L{'MIFLORA.errAdapterInventory'};
    }
} else {
    push @errors, $L{'MIFLORA.errAdapterInventory'};
}

my @adapter_ids = ('default', 'usb', 'serial');
my %adapter_labels = (
    default => 'Default - hci0',
    serial => $L{'MIFLORA.adapterSerial'},
    usb => $L{'MIFLORA.adapterUSB'},
);
foreach my $kind (@adapter_ids) {
    next if $kind eq 'default';
    my ($match) = grep { $_->{kind} eq $kind } @adapters;
    $adapter_labels{$kind} = $match ? $match->{selection_label}
        : "$adapter_labels{$kind} - $L{'MIFLORA.adapterMissing'}";
}
if ($values{selBTAdapter} !~ /\A(?:default|serial|usb)\z/) {
    my $legacy = $values{selBTAdapter} eq 'default' ? 'hci0' : $values{selBTAdapter};
    my ($match) = grep { $_->{id} eq $legacy && $_->{kind} =~ /^(?:usb|serial)$/ } @adapters;
    $values{selBTAdapter} = $match ? $match->{kind} : 'serial';
    push @errors, $L{'MIFLORA.errAdapterMigration'};
}

if ($cgi->request_method eq 'POST' && $cgi->param('btnSave')) {
    my @validation_errors;
    foreach my $field (keys %fields) {
        $values{$field} = scalar($cgi->param($field)) // '';
        push @validation_errors, $L{'MIFLORA.errInvalidInput'} if $values{$field} =~ /[\r\n\0]/;
    }
    foreach my $field ('chkPluginEnabled', 'chkLocaltime', 'chkMQTTEnabled', 'chkUDPEnabled') {
        push @validation_errors, $L{'MIFLORA.errInvalidInput'} unless $values{$field} =~ /\A[01]\z/;
    }
    push @validation_errors, $L{'MIFLORA.errInvalidInput'}
        unless $values{selPollFrequency} =~ /\A(?:1|2|4|8|12|24)\z/;
    push @validation_errors, $L{'MIFLORA.errInvalidInput'}
        unless $values{selBTAdapter} =~ /\A(?:default|serial|usb)\z/;
    push @validation_errors, $L{'MIFLORA.errOutputRequired'}
        if $values{chkMQTTEnabled} ne '1' && $values{chkUDPEnabled} ne '1';
    if ($values{chkMQTTEnabled} eq '1') {
        push @validation_errors, $L{'MIFLORA.errMQTT'}
            if $values{txtMQTTTopic} !~ /[^\/\s]/
            || $values{txtMQTTTopic} =~ /[+#]/;
    }
    if ($values{chkUDPEnabled} eq '1') {
        push @validation_errors, $L{'MIFLORA.errPort'} unless valid_port($values{txtMsUDPPort});
        my $server = $values{selMiniServer};
        $server =~ s/^MINISERVER//;
        push @validation_errors, $L{'MIFLORA.errMiniserver'} unless exists $miniservers{$server};
    }
    if (@validation_errors) {
        push @errors, @validation_errors;
    } else {
        foreach my $field (keys %fields) {
            $pcfg->param("MIFLORA.$fields{$field}[0]", $values{$field});
        }
        foreach my $key ('MQTTBROKER', 'MQTTPORT', 'MQTTUSERNAME', 'MQTTPASSWORD') {
            $pcfg->delete("MIFLORA.$key");
        }
        local $Config::Simple::USEQQ = 1;
        if ($pcfg->save()) {
            $message = $L{'MIFLORA.msgSaved'};
        } else {
            push @errors, $L{'MIFLORA.errSave'};
        }
    }
}

if ($cgi->request_method eq 'POST' && $cgi->param('btnPoll')) {
    my $pid = fork();
    if (!defined $pid) {
        push @errors, $L{'MIFLORA.errPoll'};
    } elsif ($pid == 0) {
        setsid() >= 0 or die "Cannot detach MiFlora poll: $!";
        open(STDIN, '<', '/dev/null') or die $!;
        open(STDOUT, '>>', "$lbplogdir/xiaomi-miflora.log") or die $!;
        open(STDERR, '>&', \*STDOUT) or die $!;
        exec('/bin/sh', "$lbpbindir/run.sh",
            '--configfile', "$lbpconfigdir/miflora.cfg",
            '--logfile', "$lbplogdir/xiaomi-miflora.log",
            '--datafile', "$lbpdatadir/lastdata.dat")
            or die "Cannot start MiFlora poll: $!";
    } else {
        $message = $L{'MIFLORA.txtPollingMessage'};
    }
}

$template->param(frmStart => $cgi->start_form(-name => 'MiFloraPlugin', -method => 'POST'));
$template->param(frmEnd => $cgi->end_form());
foreach my $field ('chkPluginEnabled', 'chkMQTTEnabled', 'chkUDPEnabled', 'chkLocaltime') {
    $template->param($field => $cgi->popup_menu(
        -name => $field, -id => $field, -values => ['0', '1'],
        -labels => {'0' => 'Off', '1' => 'On'}, -default => $values{$field}, -override => 1,
    ));
}
foreach my $field ('txtMQTTTopic', 'txtMsUDPPort') {
    $template->param($field => $cgi->textfield(
        -name => $field, -id => $field, -default => $values{$field}, -override => 1,
    ));
}
$template->param(selBTAdapter => $cgi->popup_menu(
    -name => 'selBTAdapter', -id => 'selBTAdapter', -values => \@adapter_ids,
    -labels => \%adapter_labels, -default => $values{selBTAdapter}, -override => 1,
));
my @servers = map { "MINISERVER$_" } sort keys %miniservers;
my %server_labels = map { ("MINISERVER$_" => $miniservers{$_}{Name}) } keys %miniservers;
$template->param(selMiniServer => $cgi->popup_menu(
    -name => 'selMiniServer', -id => 'selMiniServer', -values => \@servers,
    -labels => \%server_labels, -default => $values{selMiniServer}, -override => 1,
));
my %frequency_labels = map { ($_ => $L{"MIFLORA.selPollFrequency${_}h"}) } (1, 2, 4, 8, 12, 24);
$template->param(selPollFrequency => $cgi->popup_menu(
    -name => 'selPollFrequency', -id => 'selPollFrequency', -values => [1, 2, 4, 8, 12, 24],
    -labels => \%frequency_labels, -default => $values{selPollFrequency}, -override => 1,
));
foreach my $button ('btnSave', 'btnPoll') {
    $template->param($button => $cgi->submit(-name => $button, -value => $L{"MIFLORA.$button"}, "-data-inline" => "true"));
}
$template->param(btnRefresh => $cgi->submit(-name => 'btnRefresh', -value => $L{'MIFLORA.btnRefresh'}, "-data-inline" => "true"));
my $lastdata = "";
if (-e "$lbpdatadir/lastdata.dat") {
    open(my $handle, '<', "$lbpdatadir/lastdata.dat") or die "Cannot read last data: $!";
    $lastdata = do { local $/; <$handle> };
    close($handle);
}
$template->param(txtLastData => $cgi->textarea(
    -name => 'txtLastData', -id => 'txtLastData', -rows => 12, -columns => 60,
    -readonly => 'readonly', -default => $lastdata, -override => 1,
));
$template->param(adapters => [map { {label => $_->{label}} } @adapters]);
$template->param(errors => [map { {error => $_} } @errors], message => $message);
print $template->output();
LoxBerry::Web::lbfooter();

sub valid_port {
    my ($port) = @_;
    return $port =~ /\A[0-9]+\z/ && $port >= 1 && $port <= 65535;
}
