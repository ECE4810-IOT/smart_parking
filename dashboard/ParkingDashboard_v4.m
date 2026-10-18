function ParkingDashboard_v4
% ParkingDashboard_v4  SmartPark analytics dashboard (single gateway channel).
%
% Reads the ONE ThingSpeak channel written by the SmartPark gateway (fog node):
%   field1..4  bay1..bay4 status  (0 free, 1 occupied, 2 reserved, 3 unauthorised, -1 offline/fault)
%   field5     free bays
%   field6     arrivals + departures in the upload interval
%   field7     alerts in the upload interval
%   field8     average stay of departures in the interval (minutes)
%
% Features
%   * live bay map with status lamps + per-bay occupancy %, visits, avg stay, unauthorised count
%   * KPIs: free bays, average occupancy, visits, average stay, alerts, data freshness
%   * charts: occupancy over time, per-bay status timeline, arrivals by hour (peak), events & alerts
%   * timeframe 1 h / 1 day / 7 days / 30 days (reads in 1-day chunks, ThingSpeak returns max 8000 points)
%   * Demo mode: realistic simulated data so the dashboard works before the Pis are deployed
%   * Export: CSV of the data + PNG of the dashboard and every chart, for the report
%
% Run:  ParkingDashboard_v4    (channel id / read key are remembered between sessions)

% ------------------------------------------------------------------ constants
NBAYS = 4;
UPDATE_S = 20;                         % gateway uploads every 20 s
STALE_S = 90;                          % no data for this long -> gateway considered offline
CODES  = [-1 0 1 2 3];
LABELS = {'Offline', 'Free', 'Occupied', 'Reserved', 'Unauthorised'};
COLORS = [0.60 0.60 0.60;              % offline
          0.13 0.70 0.33;              % free
          0.86 0.20 0.20;              % occupied
          0.96 0.62 0.04;              % reserved
          0.58 0.12 0.62];             % unauthorised
NODATA_COLOR = [0.85 0.85 0.85];

% ------------------------------------------------------------------ state
chanID = getpref('SmartParkDashboard', 'channelID', []);
readKey = getpref('SmartParkDashboard', 'readKey', '');
demoMode = isempty(chanID);
busy = false;
lastX = datetime.empty(0, 1);
lastF = zeros(0, 8);

% ------------------------------------------------------------------ figure
fig = uifigure('Name', 'SmartPark Dashboard', 'Position', [60 60 1400 860], 'Color', [1 1 1]);
main = uigridlayout(fig, [4 1]);
main.RowHeight = {'fit', 96, 128, '1x'};
main.Padding = [10 10 10 10];
main.RowSpacing = 8;

% --- row 1: toolbar --------------------------------------------------------
bar1 = uigridlayout(main, [1 11]);
bar1.ColumnWidth = {'fit', 110, 'fit', 170, 'fit', 'fit', 'fit', 120, 'fit', 'fit', '1x'};
bar1.Padding = [0 0 0 0];
uilabel(bar1, 'Text', 'Channel ID:', 'FontWeight', 'bold');
idEdit = uieditfield(bar1, 'numeric', 'ValueDisplayFormat', '%.0f', 'AllowEmpty', 'on');
if ~isempty(chanID), idEdit.Value = chanID; end
uilabel(bar1, 'Text', 'Read key:', 'FontWeight', 'bold');
keyEdit = uieditfield(bar1, 'text', 'Value', readKey, 'Placeholder', 'blank if channel is public');
uibutton(bar1, 'push', 'Text', 'Connect', 'FontWeight', 'bold', ...
    'BackgroundColor', [0.85 0.92 1], 'ButtonPushedFcn', @(~, ~) onConnect());
demoBox = uicheckbox(bar1, 'Text', 'Demo data', 'Value', demoMode, 'ValueChangedFcn', @(~, ~) onDemoToggled());
uilabel(bar1, 'Text', 'Timeframe:', 'FontWeight', 'bold');
tfDrop = uidropdown(bar1, 'Items', {'Last 1 hour', 'Last 1 day', 'Last 7 days', 'Last 30 days'}, ...
    'ItemsData', {'1H', '1D', '7D', '30D'}, 'Value', '1D', 'ValueChangedFcn', @(~, ~) refresh());
uibutton(bar1, 'push', 'Text', 'Refresh', 'ButtonPushedFcn', @(~, ~) refresh());
uibutton(bar1, 'push', 'Text', 'Export for report', 'ButtonPushedFcn', @(~, ~) onExport());
statusLbl = uilabel(bar1, 'Text', '', 'HorizontalAlignment', 'right', 'FontColor', [0.4 0.4 0.4]);

% --- row 2: KPI tiles ------------------------------------------------------
kpiGrid = uigridlayout(main, [1 6]);
kpiGrid.Padding = [0 0 0 0];
kpiGrid.ColumnSpacing = 8;
[kFree, kFreeSub]   = makeTile(kpiGrid, 'Free bays now');
[kOcc, kOccSub]     = makeTile(kpiGrid, 'Average occupancy');
[kVisits, kVisSub]  = makeTile(kpiGrid, 'Vehicle visits');
[kStay, kStaySub]   = makeTile(kpiGrid, 'Average stay');
[kAlerts, kAlSub]   = makeTile(kpiGrid, 'Alerts');
[kFresh, kFreshSub] = makeTile(kpiGrid, 'Gateway data');

% --- row 3: bay tiles ------------------------------------------------------
bayGrid = uigridlayout(main, [1 NBAYS]);
bayGrid.Padding = [0 0 0 0];
bayGrid.ColumnSpacing = 8;
bayLamp = gobjects(1, NBAYS);
bayState = gobjects(1, NBAYS);
bayStats = gobjects(1, NBAYS);
for b = 1:NBAYS
    p = uipanel(bayGrid, 'Title', sprintf('Bay %d', b), 'FontWeight', 'bold', 'FontSize', 13);
    g = uigridlayout(p, [2 2]);
    g.ColumnWidth = {56, '1x'};
    g.RowHeight = {'1x', 'fit'};
    g.Padding = [8 4 8 6];
    bayLamp(b) = uilamp(g, 'Color', NODATA_COLOR);
    bayLamp(b).Layout.Row = [1 2];
    bayState(b) = uilabel(g, 'Text', 'No data', 'FontSize', 18, 'FontWeight', 'bold');
    bayStats(b) = uilabel(g, 'Text', '', 'FontColor', [0.35 0.35 0.35], 'WordWrap', 'on');
end

% --- row 4: charts ---------------------------------------------------------
chartGrid = uigridlayout(main, [2 2]);
chartGrid.Padding = [0 0 0 0];
chartGrid.RowSpacing = 8;
chartGrid.ColumnSpacing = 8;
axOcc = uiaxes(chartGrid);    title(axOcc, 'Occupied vs free bays over time');
axGantt = uiaxes(chartGrid);  title(axGantt, 'Bay status timeline');
axHour = uiaxes(chartGrid);   title(axHour, 'Arrivals by hour of day (peak hour)');
axEvents = uiaxes(chartGrid); title(axEvents, 'Parking events and alerts');

% ------------------------------------------------------------------ timer
t = timer('ExecutionMode', 'fixedSpacing', 'Period', UPDATE_S, 'BusyMode', 'drop', ...
    'TimerFcn', @(~, ~) refresh(), 'ErrorFcn', @(~, e) warning('Dashboard timer: %s', e.Data.message));
fig.CloseRequestFcn = @(src, ~) closeApp(src);
refresh();
start(t);

% =========================================================================
% UI callbacks
% =========================================================================
    function onConnect()
        v = idEdit.Value;
        if isempty(v) || isnan(v) || v <= 0
            uialert(fig, 'Enter the numeric ThingSpeak channel id of the gateway channel.', 'Channel id');
            return;
        end
        chanID = v;
        readKey = strtrim(keyEdit.Value);
        setpref('SmartParkDashboard', 'channelID', chanID);
        setpref('SmartParkDashboard', 'readKey', readKey);
        demoMode = false;
        demoBox.Value = false;
        refresh();
    end

    function onDemoToggled()
        if ~demoBox.Value && isempty(chanID)
            demoBox.Value = true;
            uialert(fig, 'Connect to a channel first, or keep demo data on.', 'No channel');
            return;
        end
        demoMode = demoBox.Value;
        refresh();
    end

    function closeApp(src)
        if isvalid(t)
            stop(t);
            delete(t);
        end
        delete(src);
    end

% =========================================================================
% Data
% =========================================================================
    function [t0, t1] = getWindow()
        t1 = datetime('now', 'TimeZone', 'local');
        switch tfDrop.Value
            case '1H',  t0 = t1 - hours(1);
            case '1D',  t0 = t1 - days(1);
            case '7D',  t0 = t1 - days(7);
            otherwise,  t0 = t1 - days(30);
        end
    end

    function [x, F] = fetch(t0, t1)
        % thingSpeakRead returns at most 8000 points -> read in 1-day chunks (20 s uploads = 4320/day)
        x = datetime.empty(0, 1);
        x.TimeZone = 'local';              % so zoned timestamps can be concatenated
        F = zeros(0, 8);
        s = t0;
        while s < t1
            e = min(s + days(1), t1);
            args = {'Fields', 1:8, 'DateRange', [s, e]};
            if ~isempty(readKey)
                args = [args, {'ReadKey', readKey}]; %#ok<AGROW>
            end
            [d, ts] = thingSpeakRead(chanID, args{:});
            if ~isempty(d)
                if size(d, 2) < 8
                    d(:, end+1:8) = NaN;
                end
                ts = ts(:);
                if ~isdatetime(ts)
                    ts = datetime(ts, 'ConvertFrom', 'datenum');
                end
                ts.TimeZone = 'local';         % unzoned -> local; UTC -> converted to local clock
                x = [x; ts]; %#ok<AGROW>
                F = [F; d(:, 1:8)]; %#ok<AGROW>
            end
            s = e;
        end
        [x, iu] = unique(x);
        F = F(iu, :);
        x.TimeZone = '';
    end

    function [x, F] = demoData(t0, t1)
        % Realistic simulated gateway data (fixed seed per window so it does not flicker).
        rs = RandStream('mt19937ar', 'Seed', 42);
        span = seconds(t1 - t0);
        step = max(UPDATE_S, span / 4000);
        x = (t0:seconds(step):t1)';
        n = numel(x);
        F = nan(n, 8);
        hr = hour(x) + minute(x) / 60;
        demand = 0.15 + 0.85 * (exp(-((hr - 9.5) / 1.5).^2) + 0.8 * exp(-((hr - 14) / 1.8).^2));
        demand(hr < 6.5 | hr > 21) = 0.02;
        st = zeros(1, NBAYS);
        leaveAt = NaT(1, NBAYS);
        arrivedAt = NaT(1, NBAYS);
        for k = 1:n
            ev = 0; al = 0; dwellSum = 0; nDep = 0;
            for bb = 1:NBAYS
                switch st(bb)
                    case 0      % free -> reserved (kiosk) or unauthorised arrival
                        if rand(rs) < demand(k) * step / 1800
                            st(bb) = 2;
                            leaveAt(bb) = x(k) + minutes(1 + 4 * rand(rs));   % drive to bay
                        elseif rand(rs) < demand(k) * step / 60000
                            st(bb) = 3; ev = ev + 1; al = al + 1;   % unauthorised car -> alert
                            arrivedAt(bb) = x(k);
                            leaveAt(bb) = x(k) + minutes(5 + 25 * rand(rs));
                        end
                    case 2      % reserved -> car arrives
                        if x(k) >= leaveAt(bb)
                            st(bb) = 1; ev = ev + 1;
                            arrivedAt(bb) = x(k);
                            leaveAt(bb) = x(k) + minutes(-45 * log(rand(rs)) + 5);   % ~45 min stays
                        end
                    case {1, 3} % occupied -> departs
                        if x(k) >= leaveAt(bb)
                            st(bb) = 0; ev = ev + 1; nDep = nDep + 1;
                            dwellSum = dwellSum + minutes(x(k) - arrivedAt(bb));
                        end
                end
            end
            F(k, 1:NBAYS) = st;
            F(k, 5) = sum(st == 0);
            F(k, 6) = ev;
            F(k, 7) = al;
            if nDep > 0, F(k, 8) = dwellSum / nDep; end
        end
        off = x > t0 + 0.62 * (t1 - t0) & x < t0 + 0.64 * (t1 - t0);   % short bay-4 outage
        F(off, 4) = -1;
        x.TimeZone = '';
    end

% =========================================================================
% Refresh + analytics
% =========================================================================
    function refresh()
        if busy || ~isvalid(fig), return; end
        busy = true;
        cleanupObj = onCleanup(@() setBusyFalse()); %#ok<NASGU>
        [t0, t1] = getWindow();
        statusLbl.Text = 'Loading...';
        drawnow limitrate;
        try
            if demoMode
                [x, F] = demoData(t0, t1);
                src = 'DEMO DATA';
            else
                [x, F] = fetch(t0, t1);
                src = sprintf('Channel %.0f', chanID);
            end
        catch ME
            statusLbl.Text = ['Read failed: ' ME.message];
            statusLbl.FontColor = [0.8 0 0];
            return;
        end
        lastX = x;
        lastF = F;
        t0.TimeZone = '';
        t1.TimeZone = '';
        render(x, F, t0, t1);
        statusLbl.FontColor = [0.4 0.4 0.4];
        statusLbl.Text = sprintf('%s  |  %d samples  |  updated %s', src, numel(x), ...
            char(datetime('now', 'Format', 'HH:mm:ss')));
    end

    function setBusyFalse()
        busy = false;
    end

    function render(x, F, t0, t1)
        S = F(:, 1:NBAYS);
        if isempty(x)
            clearAll(t0, t1);
            return;
        end
        % sample durations (time-weighted statistics); last sample counts as one interval
        dt = seconds(diff(x));
        dt(end+1, 1) = median([dt; UPDATE_S]);
        dt = min(dt, 5 * UPDATE_S);        % gaps (gateway off) do not count as occupied time

        occMask = (S == 1 | S == 3);
        valid = ~isnan(S) & S ~= -1;
        visits = zeros(1, NBAYS); unauth = zeros(1, NBAYS); occPct = nan(1, NBAYS);
        stays = cell(1, NBAYS); arrivalTimes = datetime.empty(0, 1);
        for bb = 1:NBAYS
            o = occMask(:, bb);
            if any(valid(:, bb))
                occPct(bb) = 100 * sum(dt(o & valid(:, bb))) / sum(dt(valid(:, bb)));
            end
            rise = find(o & [false; ~o(1:end-1)]);
            fall = find(~o & [false; o(1:end-1)] & valid(:, bb));
            visits(bb) = numel(rise);
            arrivalTimes = [arrivalTimes; x(rise)]; %#ok<AGROW>
            s3 = S(:, bb) == 3;
            unauth(bb) = sum(s3 & [true; ~s3(1:end-1)]);
            d = [];
            for r = rise'
                f = fall(find(fall > r, 1));
                if ~isempty(f)
                    d(end+1) = minutes(x(f) - x(r)); %#ok<AGROW>
                end
            end
            stays{bb} = d;
        end
        allStays = [stays{:}];

        % ---- bay tiles
        for bb = 1:NBAYS
            last = find(~isnan(S(:, bb)), 1, 'last');
            if isempty(last)
                bayLamp(bb).Color = NODATA_COLOR;
                bayState(bb).Text = 'No data';
            else
                ci = find(CODES == S(last, bb), 1);
                if isempty(ci), ci = 1; end
                bayLamp(bb).Color = COLORS(ci, :);
                bayState(bb).Text = LABELS{ci};
            end
            if isempty(stays{bb}), avgS = '-'; else, avgS = fmtMin(mean(stays{bb})); end
            bayStats(bb).Text = sprintf('Occupancy %s  |  Visits %d  |  Avg stay %s  |  Unauthorised %d', ...
                fmtPct(occPct(bb)), visits(bb), avgS, unauth(bb));
        end

        % ---- KPIs
        lastFree = F(find(~isnan(F(:, 5)), 1, 'last'), 5);
        if isempty(lastFree)
            kFree.Text = '-';
        else
            kFree.Text = sprintf('%d / %d', lastFree, NBAYS);
            kFree.FontColor = ternary(lastFree == 0, [0.8 0 0], [0 0.55 0.2]);
        end
        nOcc = sum(occMask, 2);
        [~, pk] = max(nOcc);
        kFreeSub.Text = sprintf('Busiest: %d occupied at %s', nOcc(pk), char(string(x(pk), 'dd MMM HH:mm')));
        kOcc.Text = fmtPct(mean(occPct, 'omitnan'));
        kOccSub.Text = 'time-weighted, all bays';
        kVisits.Text = sprintf('%d', sum(visits));
        kVisSub.Text = sprintf('%.2f visits per bay', sum(visits) / NBAYS);
        if isempty(allStays)
            w = F(:, 8); w = w(~isnan(w));
            if isempty(w), kStay.Text = '-'; else, kStay.Text = fmtMin(mean(w)); end
            kStaySub.Text = 'no completed stays in window';
        else
            kStay.Text = fmtMin(mean(allStays));
            kStaySub.Text = sprintf('median %s, max %s', fmtMin(median(allStays)), fmtMin(max(allStays)));
        end
        nAl = sum(F(:, 7), 'omitnan');
        kAlerts.Text = sprintf('%d', nAl);
        kAlerts.FontColor = ternary(nAl > 0, [0.8 0 0], [0 0 0]);
        kAlSub.Text = sprintf('%d unauthorised arrivals', sum(unauth));
        age = seconds(datetime('now') - x(end));
        if age > STALE_S
            kFresh.Text = 'OFFLINE';
            kFresh.FontColor = [0.8 0 0];
            kFreshSub.Text = sprintf('last data %s ago', fmtMin(age / 60));
        else
            kFresh.Text = 'Live';
            kFresh.FontColor = [0 0.55 0.2];
            kFreshSub.Text = sprintf('last update %.0f s ago', age);
        end

        % ---- chart 1: occupied vs free over time
        cla(axOcc); hold(axOcc, 'on');
        stairs(axOcc, x, nOcc, 'LineWidth', 1.8, 'Color', COLORS(3, :));
        stairs(axOcc, x, F(:, 5), 'LineWidth', 1.2, 'Color', COLORS(2, :));
        hold(axOcc, 'off');
        ylim(axOcc, [-0.2 NBAYS + 0.2]); yticks(axOcc, 0:NBAYS);
        xlim(axOcc, [t0 t1]); ylabel(axOcc, 'Bays');
        legend(axOcc, {'Occupied', 'Free'}, 'Location', 'northwest');
        grid(axOcc, 'on');

        % ---- chart 2: per-bay status timeline (one coloured segment per status run)
        cla(axGantt); hold(axGantt, 'on');
        for bb = 1:NBAYS
            s = S(:, bb);
            s(isnan(s)) = -2;
            edges = [1; find(diff(s) ~= 0) + 1; numel(s) + 1];
            for r = 1:numel(edges) - 1
                i0 = edges(r); i1 = min(edges(r + 1), numel(x));
                ci = find(CODES == s(i0), 1);
                if isempty(ci), col = NODATA_COLOR; else, col = COLORS(ci, :); end
                plot(axGantt, [x(i0) x(i1)], [bb bb], '-', 'Color', col, 'LineWidth', 14);
            end
        end
        for ci = 1:numel(CODES)      % legend entries
            plot(axGantt, NaT, NaN, 's', 'MarkerFaceColor', COLORS(ci, :), 'MarkerEdgeColor', 'none', ...
                'MarkerSize', 9, 'DisplayName', LABELS{ci});
        end
        hold(axGantt, 'off');
        h = findobj(axGantt, 'Marker', 's');
        legend(axGantt, flipud(h), 'Location', 'southoutside', 'Orientation', 'horizontal');
        ylim(axGantt, [0.4 NBAYS + 0.6]); yticks(axGantt, 1:NBAYS);
        yticklabels(axGantt, arrayfun(@(k) sprintf('Bay %d', k), 1:NBAYS, 'UniformOutput', false));
        axGantt.YDir = 'reverse';
        xlim(axGantt, [t0 t1]);

        % ---- chart 3: arrivals by hour of day
        cla(axHour);
        counts = histcounts(hour(arrivalTimes), -0.5:1:23.5);
        bh = bar(axHour, 0:23, counts, 'FaceColor', 'flat', 'EdgeColor', 'none');
        bh.CData = repmat([0.55 0.7 0.9], 24, 1);
        if any(counts)
            [~, peakH] = max(counts);
            bh.CData(peakH, :) = COLORS(3, :);
            title(axHour, sprintf('Arrivals by hour of day - peak %02d:00-%02d:00', peakH - 1, peakH));
        else
            title(axHour, 'Arrivals by hour of day');
        end
        xlim(axHour, [-0.6 23.6]); xticks(axHour, 0:3:23);
        xlabel(axHour, 'Hour'); ylabel(axHour, 'Arrivals'); grid(axHour, 'on');

        % ---- chart 4: events and alerts, aggregated into ~48 bins
        cla(axEvents);
        nb = 48;
        edgesT = linspace(t0, t1, nb + 1);
        bin = discretize(x, edgesT);
        ok = ~isnan(bin);
        ev = accumarray(bin(ok), fillNaN(F(ok, 6)), [nb 1]);
        al = accumarray(bin(ok), fillNaN(F(ok, 7)), [nb 1]);
        centers = edgesT(1:end-1) + (edgesT(2) - edgesT(1)) / 2;
        b2 = bar(axEvents, centers, [ev al], 'grouped', 'EdgeColor', 'none');
        b2(1).FaceColor = [0.3 0.5 0.8];
        b2(2).FaceColor = COLORS(3, :);
        legend(axEvents, {'Arrivals + departures', 'Alerts'}, 'Location', 'northwest');
        ylabel(axEvents, sprintf('per %s', fmtMin(minutes(edgesT(2) - edgesT(1)))));
        xlim(axEvents, [t0 t1]); grid(axEvents, 'on');
        drawnow limitrate;
    end

    function clearAll(t0, t1)
        for bb = 1:NBAYS
            bayLamp(bb).Color = NODATA_COLOR;
            bayState(bb).Text = 'No data';
            bayStats(bb).Text = '';
        end
        for k = [kFree kOcc kVisits kStay kAlerts]
            k.Text = '-';
        end
        kFresh.Text = 'No data'; kFresh.FontColor = [0.8 0 0];
        kFreshSub.Text = 'nothing in this timeframe';
        for ax = [axOcc axGantt axEvents]
            cla(ax); xlim(ax, [t0 t1]);
        end
        cla(axHour);
    end

% =========================================================================
% Export (evidence for the report)
% =========================================================================
    function onExport()
        if isempty(lastX)
            uialert(fig, 'No data to export yet.', 'Export');
            return;
        end
        folder = uigetdir(pwd, 'Choose a folder for the exported files');
        if isequal(folder, 0), return; end
        stamp = char(datetime('now', 'Format', 'yyyyMMdd_HHmmss'));
        T = array2table(lastF, 'VariableNames', {'bay1', 'bay2', 'bay3', 'bay4', 'free_bays', ...
            'events', 'alerts', 'avg_stay_min'});
        T = addvars(T, lastX, 'Before', 1, 'NewVariableNames', 'time');
        writetable(T, fullfile(folder, ['smartpark_data_' stamp '.csv']));
        exportapp(fig, fullfile(folder, ['smartpark_dashboard_' stamp '.png']));
        names = {'occupancy', 'status_timeline', 'arrivals_by_hour', 'events_alerts'};
        axs = [axOcc axGantt axHour axEvents];
        for k = 1:numel(axs)
            exportgraphics(axs(k), fullfile(folder, ['smartpark_' names{k} '_' stamp '.png']), 'Resolution', 200);
        end
        uialert(fig, sprintf('Exported CSV, dashboard and 4 charts to:\n%s', folder), 'Export complete', ...
            'Icon', 'success');
    end

% =========================================================================
% Small helpers
% =========================================================================
    function [valueLbl, subLbl] = makeTile(parent, titleText)
        tp = uipanel(parent, 'Title', titleText, 'FontWeight', 'bold');
        tg = uigridlayout(tp, [2 1]);
        tg.RowHeight = {'1x', 'fit'};
        tg.Padding = [8 2 8 4];
        valueLbl = uilabel(tg, 'Text', '-', 'FontSize', 24, 'FontWeight', 'bold');
        subLbl = uilabel(tg, 'Text', '', 'FontColor', [0.4 0.4 0.4]);
    end
end

% ----------------------------------------------------------------- local functions
function s = fmtMin(m)
if isempty(m) || isnan(m)
    s = '-';
elseif m >= 60
    s = sprintf('%.1f h', m / 60);
elseif m >= 1
    s = sprintf('%.0f min', m);
else
    s = sprintf('%.0f s', m * 60);
end
end

function s = fmtPct(p)
if isempty(p) || isnan(p)
    s = '-';
else
    s = sprintf('%.0f%%', p);
end
end

function v = fillNaN(v)
v(isnan(v)) = 0;
end

function out = ternary(c, a, b)
if c, out = a; else, out = b; end
end
