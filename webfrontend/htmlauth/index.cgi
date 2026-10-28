#!/usr/bin/perl

use strict;
use warnings;
use CGI;
use Config::Simple;
use HTML::Template;
use JSON::PP ();
use POSIX qw(setsid);
use Fcntl qw(:flock F_SETFD);
use Encode qw(decode encode FB_CROAK);
use LoxBerry::System;
use LoxBerry::Web;

my $cgi = CGI->new;
my $pcfg = Config::Simple->new("$lbpconfigdir/miflora.cfg")
    or die Config::Simple->error();
my $template = HTML::Template->new(
    filename => "$lbptemplatedir/index.html",
    global_vars => 1,
    loop_context_vars => 1,
    die_on_bad_params => 0,
    utf8 => 1,
);
my %L = LoxBerry::System::readlanguage($template, "language.ini");
foreach my $key (keys %L) {
    $L{$key} = decode('UTF-8', $L{$key}, FB_CROAK) unless utf8::is_utf8($L{$key});
}
foreach my $key (qw(pluginIntro hintLocaltime hintMQTT hintMQTTTopic hintDiagnostics
        hintLogs hintLoxone gatewayAvailable gatewayUnavailable errMQTT)) {
    my $value = CGI::escapeHTML($L{"FLOWERMON.$key"});
    $value =~ s{&lt;(/?(?:b|i|code))&gt;}{<$1>}g;
    $L{"FLOWERMON.$key"} = $value;
}
foreach my $key (keys %L) {
    if ($key =~ /^FLOWERMON\.(.+)$/) {
        $template->param($1 => $L{$key});
    }
}
my $action = $cgi->url_param('action') // $cgi->param('action') // '';
if ($action ne '') {
    eval {
        if ($action eq 'poll') {
            die "POST required" unless $cgi->request_method eq 'POST';
            json_response({started => start_poll() ? JSON::PP::true : JSON::PP::false});
        } elsif ($action eq 'status') {
            my $text = status_text();
            json_response({running => poll_running() ? JSON::PP::true : JSON::PP::false,
                text => $text, sensors => sensor_addresses($text)});
        } elsif ($action eq 'clear-output') {
            die "POST required" unless $cgi->request_method eq 'POST';
            open(my $lock, '>>', "$lbpdatadir/.poll.lock") or die "Cannot open poll lock: $!";
            unless (flock($lock, LOCK_EX | LOCK_NB)) {
                json_response({error => $L{'FLOWERMON.errClearRunning'}});
            } else {
                foreach my $file ('poll-progress.log', 'lastdata.dat') {
                    open(my $handle, '>:encoding(UTF-8)', "$lbpdatadir/$file") or die "Cannot clear $file: $!";
                    close($handle) or die "Cannot close $file: $!";
                }
                json_response({text => '', sensors => []});
            }
            close($lock);
        } elsif ($action eq 'download-data') {
            download('flower-monitor-last-data.txt', status_text());
        } elsif ($action eq 'template-settings') {
            json_response(template_settings_response(read_template_settings()));
        } elsif ($action eq 'template-reset') {
            die "POST required" unless $cgi->request_method eq 'POST';
            my $settings = read_template_settings();
            delete $settings->{params};
            write_template_settings($settings);
            json_response(template_settings_response($settings));
        } elsif ($action eq 'template-export') {
            die "POST required" unless $cgi->request_method eq 'POST';
            export_template();
        } else {
            die "Unknown action";
        }
    };
    if ($@) {
        warn "Flower Monitor frontend: $@";
        print $cgi->header(-type => 'application/json', -charset => 'utf-8',
            -status => '400 Bad Request', -cache_control => 'no-store');
        print JSON::PP::encode_json({error => $L{'FLOWERMON.errRequest'}});
    }
    exit;
}
my $tab = $cgi->url_param('tab') // $cgi->param('tab') // 'settings';
$tab = 'settings' unless $tab =~ /\A(?:settings|status|logs)\z/;
our @navbar = map {
    {Name => $L{"FLOWERMON.tab$_"}, URL => "index.cgi?tab=$_",
     active => ($_ eq $tab ? JSON::PP::true : JSON::PP::false)}
} ('settings', 'status', 'logs');
LoxBerry::Web::lbheader("BLE Flower Monitor", "https://github.com/Jan21493/LoxBerry-Plugin-BleFlowerMonitor", "help.html");
binmode(STDOUT, ':encoding(UTF-8)');
$template->param(isSettings => $tab eq 'settings', isStatus => $tab eq 'status', isLogs => $tab eq 'logs');
my %miniservers = LoxBerry::System::get_miniservers();
my @errors;
if ($tab eq 'status') {
    eval {
        my $general = JSON::PP::decode_json(read_text("$LoxBerry::System::lbsconfigdir/general.json", 1));
        my $version = $general->{Mqtt}{Gatewayversion};
        my $lbversion = $general->{Base}{Version} // '';
        my ($major) = $lbversion =~ /\A(\d+)/;
        my $show_version = defined($major) && $major >= 4;
        my $v2 = $show_version && defined($version) && "$version" eq '2';
        $template->param(gatewayDetected => 1, gatewayV2 => $v2,
            showGatewayVersion => $show_version);
        my $host = $general->{Mqtt}{Brokerhost};
        $template->param(mqttConfigured => defined($host) && length($host));
        $template->param(gatewayFound => $L{defined($host) && length($host)
            ? 'FLOWERMON.gatewayAvailable' : 'FLOWERMON.gatewayUnavailable'});
    };
    if ($@) {
        warn "Cannot read MQTT Gateway configuration: $@";
        push @errors, $L{'FLOWERMON.errGatewayConfig'};
    }
}
my $message = "";
my %fields = (
    chkPluginEnabled => ['ENABLED', '1'],
    selPollFrequency => ['POLLFREQUENCY', '12'],
    chkLocaltime => ['LOCALTIME', '1'],
    selBTAdapter => ['BTADAPTER', 'serial'],
    chkMQTTEnabled => ['MQTTENABLED', '1'],
    txtMQTTTopic => ['MQTTTOPIC', 'flowermon'],
    chkUDPEnabled => ['UDPENABLED', '0'],
    selMiniServer => ['MINISERVER', 'MINISERVER1'],
    txtMsUDPPort => ['UDPPORT', '5006'],
);
my %values;
foreach my $field (keys %fields) {
    my ($key, $default) = @{$fields{$field}};
    my $saved = $pcfg->param("FLOWERMON.$key");
    $saved = $pcfg->param("MIFLORA.$key") unless defined $saved;
    $saved = join(',', @$saved) if ref($saved) eq 'ARRAY';
    $values{$field} = defined $saved ? $saved : $default;
}

my @adapters;
if (open(my $inventory, '-|', '/usr/bin/python3', "$lbpbindir/miflora.py", '--list-adapters')) {
    my $json = do { local $/; <$inventory> };
    if (close($inventory)) {
        my $decoded = eval { JSON::PP::decode_json($json) };
        if ($@ || ref($decoded) ne 'ARRAY') {
            push @errors, $L{'FLOWERMON.errAdapterInventory'};
        } else {
            @adapters = @$decoded;
        }
    } else {
        push @errors, $L{'FLOWERMON.errAdapterInventory'};
    }
} else {
    push @errors, $L{'FLOWERMON.errAdapterInventory'};
}

my @adapter_ids = ('default', 'usb', 'serial');
my %adapter_labels = (
    default => 'Default - hci0',
    serial => $L{'FLOWERMON.adapterSerial'},
    usb => $L{'FLOWERMON.adapterUSB'},
);
foreach my $kind (@adapter_ids) {
    next if $kind eq 'default';
    my ($match) = grep { $_->{kind} eq $kind } @adapters;
    $adapter_labels{$kind} = $match ? $match->{selection_label}
        : "$adapter_labels{$kind} - $L{'FLOWERMON.adapterMissing'}";
}
if ($values{selBTAdapter} !~ /\A(?:default|serial|usb)\z/) {
    my $legacy = $values{selBTAdapter} eq 'default' ? 'hci0' : $values{selBTAdapter};
    my ($match) = grep { $_->{id} eq $legacy && $_->{kind} =~ /^(?:usb|serial)$/ } @adapters;
    $values{selBTAdapter} = $match ? $match->{kind} : 'serial';
    push @errors, $L{'FLOWERMON.errAdapterMigration'};
}

if ($cgi->request_method eq 'POST' && $cgi->param('btnSave')) {
    my @validation_errors;
    foreach my $field (keys %fields) {
        $values{$field} = scalar($cgi->param($field)) // '';
        push @validation_errors, $L{'FLOWERMON.errInvalidInput'} if $values{$field} =~ /[\r\n\0]/;
    }
    foreach my $field ('chkPluginEnabled', 'chkLocaltime', 'chkMQTTEnabled', 'chkUDPEnabled') {
        push @validation_errors, $L{'FLOWERMON.errInvalidInput'} unless $values{$field} =~ /\A[01]\z/;
    }
    push @validation_errors, $L{'FLOWERMON.errInvalidInput'}
        unless $values{selPollFrequency} =~ /\A(?:1|2|4|8|12|24)\z/;
    push @validation_errors, $L{'FLOWERMON.errInvalidInput'}
        unless $values{selBTAdapter} =~ /\A(?:default|serial|usb)\z/;
    push @validation_errors, $L{'FLOWERMON.errOutputRequired'}
        if $values{chkMQTTEnabled} ne '1' && $values{chkUDPEnabled} ne '1';
    if ($values{chkMQTTEnabled} eq '1') {
        push @validation_errors, $L{'FLOWERMON.errMQTT'}
            if $values{txtMQTTTopic} !~ /[^\/\s]/
            || $values{txtMQTTTopic} =~ /[+#]/;
    }
    if ($values{chkUDPEnabled} eq '1') {
        push @validation_errors, $L{'FLOWERMON.errPort'} unless valid_port($values{txtMsUDPPort});
        my $server = $values{selMiniServer};
        $server =~ s/^MINISERVER//;
        push @validation_errors, $L{'FLOWERMON.errMiniserver'} unless exists $miniservers{$server};
    }
    if (@validation_errors) {
        push @errors, @validation_errors;
    } else {
        foreach my $field (keys %fields) {
            $pcfg->param("FLOWERMON.$fields{$field}[0]", $values{$field});
        }
        foreach my $key ('MQTTBROKER', 'MQTTPORT', 'MQTTUSERNAME', 'MQTTPASSWORD') {
            $pcfg->delete("FLOWERMON.$key");
            $pcfg->delete("MIFLORA.$key");
        }
        local $Config::Simple::USEQQ = 1;
        if ($pcfg->save()) {
            $message = $L{'FLOWERMON.msgSaved'};
        } else {
            push @errors, $L{'FLOWERMON.errSave'};
        }
    }
}

if ($cgi->request_method eq 'POST' && $cgi->param('btnPoll')) {
    eval { start_poll(); };
    if ($@) {
        warn "Cannot launch Flower Monitor poll: $@";
        push @errors, $L{'FLOWERMON.errPoll'};
    } else {
        $message = $L{'FLOWERMON.txtPollingMessage'};
    }
}

$template->param(frmStart => $cgi->start_form(-name => 'MiFloraPlugin', -id => 'MiFloraPlugin',
    -method => 'POST', -action => "index.cgi?tab=$tab", '-data-ajax' => 'false'));
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
my %frequency_labels = map { ($_ => $L{"FLOWERMON.selPollFrequency${_}h"}) } (1, 2, 4, 8, 12, 24);
$template->param(selPollFrequency => $cgi->popup_menu(
    -name => 'selPollFrequency', -id => 'selPollFrequency', -values => [1, 2, 4, 8, 12, 24],
    -labels => \%frequency_labels, -default => $values{selPollFrequency}, -override => 1,
));
foreach my $button ('btnSave', 'btnPoll') {
    $template->param($button => $cgi->submit(-name => $button, -value => $L{"FLOWERMON.$button"}, "-data-inline" => "true"));
}
my $status_text = status_text();
my $sensors = sensor_addresses($status_text);
$template->param(sensorAddresses => [map { {address => $_} } @$sensors],
    selectedSensorMac => @$sensors ? $sensors->[0] : '5C:85:7E:B0:F3:AD');
$template->param(txtLastData => $cgi->textarea(
    -name => 'txtLastData', -id => 'txtLastData', -rows => 12, -columns => 60,
    -readonly => 'readonly', -default => $status_text, -override => 1,
));
if ($tab eq 'logs') {
    my $loglist = LoxBerry::Web::loglist_html(NAME => 'FlowerCare');
    if (defined($loglist) && length($loglist)) {
        $loglist = decode('UTF-8', $loglist, FB_CROAK) unless utf8::is_utf8($loglist);
        $template->param(loglist => $loglist);
    } else {
        warn "Cannot render LoxBerry FlowerCare log list\n";
        push @errors, $L{'FLOWERMON.errLogList'};
    }
}
$template->param(topicPrefix => $values{txtMQTTTopic});
$template->param(
    transportMQTTSelected => $values{chkMQTTEnabled} eq '1' || $values{chkUDPEnabled} ne '1',
    transportUDPSelected => $values{chkMQTTEnabled} ne '1' && $values{chkUDPEnabled} eq '1',
);
$template->param(adapters => [map { {label => $_->{label}} } @adapters]);
$template->param(errors => [map { {error => $_} } @errors], message => $message);
print $template->output();
binmode(STDOUT, ':raw');
LoxBerry::Web::lbfooter();

sub valid_port {
    my ($port) = @_;
    return $port =~ /\A[0-9]+\z/ && $port >= 1 && $port <= 65535;
}

sub read_text {
    my ($file, $required) = @_;
    return '' if !$required && !-e $file;
    open(my $handle, '<:encoding(UTF-8)', $file) or die "Cannot read $file: $!";
    local $/;
    my $text = <$handle> // '';
    close($handle);
    return $text;
}

sub status_text {
    my $progress = read_text("$lbpdatadir/poll-progress.log");
    return length($progress) ? $progress : read_text("$lbpdatadir/lastdata.dat");
}

sub sensor_addresses {
    my ($text) = @_;
    my %seen;
    my @addresses;
    while ($text =~ /\bDiscovered ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}) \(/g) {
        my $address = uc($1);
        push @addresses, $address unless $seen{$address}++;
    }
    return \@addresses;
}

sub poll_running {
    open(my $lock, '>>', "$lbpdatadir/.poll.lock") or die "Cannot open poll lock: $!";
    my $running = !flock($lock, LOCK_EX | LOCK_NB);
    close($lock);
    return $running;
}

sub start_poll {
    open(my $lock, '>>', "$lbpdatadir/.poll.lock") or die "Cannot open poll lock: $!";
    unless (flock($lock, LOCK_EX | LOCK_NB)) { close($lock); return 0; }
    foreach my $file ('poll-progress.log', 'lastdata.dat') {
        open(my $handle, '>:encoding(UTF-8)', "$lbpdatadir/$file") or die "Cannot clear $file: $!";
        print $handle $file eq 'poll-progress.log' ? "$L{'FLOWERMON.txtPollingMessage'}\n" : '';
        close($handle) or die "Cannot close $file: $!";
    }
    my $pid = fork();
    die "Cannot fork: $!" unless defined $pid;
    if ($pid == 0) {
        setsid() >= 0 or die "Cannot detach poll: $!";
        open(STDIN, '<', '/dev/null') or die "Cannot prepare poll stream: $!";
        open(STDOUT, '>>', "$lbplogdir/xiaomi-miflora.log") or die "Cannot prepare poll stream: $!";
        open(STDERR, '>&', \*STDOUT) or die "Cannot prepare poll stream: $!";
        # Keep the same lock across CGI -> shell -> Python to avoid a launch race.
        fcntl($lock, F_SETFD, 0) or die "Cannot inherit poll lock: $!";
        exec('/bin/sh', "$lbpbindir/run.sh", '--lock-fd', fileno($lock),
            '--configfile', "$lbpconfigdir/miflora.cfg",
            '--logfile', "$lbplogdir/xiaomi-miflora.log",
            '--datafile', "$lbpdatadir/lastdata.dat") or die "Cannot start poll: $!";
    }
    close($lock);
    return 1;
}

sub json_response {
    my ($value) = @_;
    print $cgi->header(-type => 'application/json', -charset => 'utf-8', -cache_control => 'no-store');
    print JSON::PP::encode_json($value);
}

sub download {
    my ($name, $text) = @_;
    print $cgi->header(-type => 'text/plain', -charset => 'utf-8',
        -attachment => $name, -cache_control => 'no-store');
    binmode(STDOUT, ':encoding(UTF-8)');
    print $text;
}

# Defaults taken from a Loxone Config export of the matching virtual HTTP input commands.
sub template_parameters {
    return (
        {key => 'moisture', lang => 'tplMoisture', unit => '<v>% rH', signed => 'false'},
        {key => 'battery', lang => 'tplBattery', unit => '<v> %', signed => 'true'},
        {key => 'conductivity', lang => 'tplConductivity', unit => "<v> \x{b5}S/cm", signed => 'false'},
        {key => 'light', lang => 'tplLight', unit => '<v> lx', signed => 'false'},
        {key => 'temperature', lang => 'tplTemperature', unit => "<v.1>\x{b0}C", signed => 'true'},
        {key => 'polltime', lang => 'tplPolltime', unit => '<v.u>', signed => 'false'},
    );
}

sub template_settings_file { return "$lbpconfigdir/loxone-template.json"; }

sub read_template_settings {
    my $file = template_settings_file();
    return {} unless -e $file;
    my $settings = eval { JSON::PP->new->decode(read_text($file, 1)) };
    if ($@ || ref($settings) ne 'HASH') {
        warn "Ignoring invalid Loxone template settings $file: $@";
        return {};
    }
    return $settings;
}

sub write_template_settings {
    my ($settings) = @_;
    my $file = template_settings_file();
    open(my $handle, '>:encoding(UTF-8)', "$file.tmp") or die "Cannot write $file.tmp: $!";
    print $handle JSON::PP->new->canonical->pretty->encode($settings);
    close($handle) or die "Cannot close $file.tmp: $!";
    rename("$file.tmp", $file) or die "Cannot replace $file: $!";
}

sub template_settings_response {
    my ($settings) = @_;
    my $saved = ref($settings->{params}) eq 'HASH' ? $settings->{params} : {};
    my @params;
    foreach my $param (template_parameters()) {
        my $stored = ref($saved->{$param->{key}}) eq 'HASH' ? $saved->{$param->{key}} : {};
        my $label = $L{"FLOWERMON.$param->{lang}"};
        push @params, {key => $param->{key}, defaultLabel => $label, defaultUnit => $param->{unit},
            label => $stored->{label} // $label, unit => $stored->{unit} // $param->{unit}};
    }
    return {params => \@params, names => ref($settings->{names}) eq 'HASH' ? $settings->{names} : {}};
}

sub template_text {
    my ($name, $max) = @_;
    my @values = $cgi->can('multi_param') ? $cgi->multi_param($name) : $cgi->param($name);
    my @decoded;
    foreach my $value (@values) {
        $value = eval { decode('UTF-8', $value // '', FB_CROAK) };
        return undef unless defined $value;
        $value =~ s/\A\s+|\s+\z//g;
        return undef if length($value) > $max || $value =~ /[\x00-\x1f\x7f]/;
        push @decoded, $value;
    }
    return \@decoded;
}

sub xml_attribute {
    my ($value) = @_;
    $value =~ s/&/&amp;/g;
    $value =~ s/</&lt;/g;
    $value =~ s/>/&gt;/g;
    $value =~ s/"/&quot;/g;
    return $value;
}

sub export_template {
    my $macs = template_text('mac', 17);
    my $names = template_text('name', 64);
    my $valid = $macs && $names && @$macs && @$macs == @$names;
    my (%labels, %units);
    foreach my $param (template_parameters()) {
        my $label = template_text("label_$param->{key}", 64);
        my $unit = template_text("unit_$param->{key}", 32);
        $valid &&= $label && $unit && @$label == 1 && @$unit == 1;
        last unless $valid;
        $labels{$param->{key}} = length($label->[0]) ? $label->[0] : $L{"FLOWERMON.$param->{lang}"};
        $units{$param->{key}} = $unit->[0];
    }
    my %seen;
    my @sensors;
    if ($valid) {
        foreach my $index (0 .. $#$macs) {
            my $mac = uc($macs->[$index]);
            $mac =~ s/://g if $mac =~ /\A(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\z/;
            unless ($mac =~ /\A[0-9A-F]{12}\z/) { $valid = 0; last; }
            next if $seen{$mac}++;
            push @sensors, {mac => $mac, name => $names->[$index]};
        }
    }
    unless ($valid && @sensors) {
        print $cgi->header(-type => 'application/json', -charset => 'utf-8',
            -status => '400 Bad Request', -cache_control => 'no-store');
        print JSON::PP::encode_json({error => $L{'FLOWERMON.errTemplateInput'}});
        return;
    }

    my $settings = read_template_settings();
    $settings->{names} = {} unless ref($settings->{names}) eq 'HASH';
    $settings->{names}{join(':', unpack('(A2)*', $_->{mac}))} = $_->{name} foreach @sensors;
    $settings->{params} = {map { ($_ => {label => $labels{$_}, unit => $units{$_}}) } keys %labels};
    write_template_settings($settings);

    my $topic = $pcfg->param('FLOWERMON.MQTTTOPIC') // $pcfg->param('MIFLORA.MQTTTOPIC') // 'flowermon';
    $topic = join(',', @$topic) if ref($topic) eq 'ARRAY';
    $topic = 'flowermon' unless length($topic);
    my $title = 'BLE Flower Monitor MQTT Inputs';
    my @lines = ('<?xml version="1.0" encoding="utf-8"?>',
        qq(<VirtualInHttp Title="$title" Comment="" Address="." HintText="" PollingTime="604800">),
        qq(\t<Info templateType="2" minVersion="14051207"/>));
    foreach my $sensor (@sensors) {
        my $prefix = length($sensor->{name})
            ? $sensor->{name} : join(':', unpack('(A2)*', $sensor->{mac}));
        foreach my $param (template_parameters()) {
            my $input = lc("$topic/$sensor->{mac}/$param->{key}");
            $input =~ s{[/ %]}{_}g;
            push @lines, sprintf(qq(\t<VirtualInHttpCmd Title="%s" Comment="%s" Check="\\." Signed="%s" )
                . qq(Analog="true" SourceValLow="0" DestValLow="0" SourceValHigh="100" DestValHigh="100" )
                . qq(DefVal="0" MinVal="-10000" MaxVal="10000" Unit="%s" HintText=""/>),
                xml_attribute($input), xml_attribute("$prefix $labels{$param->{key}}"),
                $param->{signed}, xml_attribute($units{$param->{key}}));
        }
    }
    push @lines, '</VirtualInHttp>';
    my $xml = encode('UTF-8', "\x{feff}" . join("\r\n", @lines) . "\r\n");
    my $filename = "VI_$title " . POSIX::strftime('%Y-%m-%d', localtime) . '.xml';

    my $directory = "$lbptemplatedir/loxone";
    my $stored = eval {
        mkdir($directory) or die "Cannot create $directory: $!" unless -d $directory;
        open(my $handle, '>:raw', "$directory/$filename") or die "Cannot write $directory/$filename: $!";
        print $handle $xml;
        close($handle) or die "Cannot close $directory/$filename: $!";
        1;
    };
    warn "Cannot store Loxone template: $@" unless $stored;
    print $cgi->header(-type => 'application/xml', -charset => 'utf-8', -attachment => $filename,
        -cache_control => 'no-store', ($stored ? (-X_Template_File => "$directory/$filename") : ()));
    binmode(STDOUT, ':raw');
    print $xml;
}
