# ffplay-quad-split

This tool creates multi-pane video wall displays using ffplay or mpv, designed for low-latency camera monitoring.

## Key Features

- Multi-monitor support with automatic layout calculation
- Configurable split layouts (1x1, 2x2, etc.)
- Automatic restart of failed camera panes
- Hardware acceleration support (with caveats for multi-stream setups)
- Low-latency video processing

## VA-API Considerations

When using multiple streams on a VA-API enabled system, conflicts can occur. The solution is to:

1. Disable hardware decoding when running more than one stream simultaneously
2. Use software decoding for better stability with multiple streams

For this reason, the `config.vaapi-safe.json` file is provided which disables hardware acceleration.

## Usage

```
# View connected displays
python3 watch_cameras.py --print-monitors

# Validate configuration
python3 watch_cameras.py -c config.json --validate

# Preview commands (dry run)
python3 watch_cameras.py -c config.json --preview

# Run the camera wall
python3 watch_cameras.py -c config.json
```

## Configuration

The main configuration file uses JSON format. The included `config.example.json` shows all available options.

For multi-stream setups on VA-API systems, use:
```bash
python3 watch_cameras.py -c config.vaapi-safe.json
```

## Troubleshooting

If you're experiencing crashes or instability with multiple streams:

1. Use the vaapi-safe configuration: `config.vaapi-safe.json`
2. Set `"hwdec": false` in your JSON config
3. Ensure that only one stream uses hardware acceleration at a time
4. Monitor system resources (CPU/GPU usage)

## Requirements

- Python 3.6+
- ffplay or mpv (for video playback)
- x11-utils (`xprop`, `xwininfo`, `xdotool` recommended)
- RTSP-compatible cameras