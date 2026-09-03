import { VoicePlayer } from "@mail/discuss/voice_message/common/voice_player";
import { patch } from "@web/core/utils/patch";
import { browser } from "@web/core/browser/browser";

// WhatsApp-style voice-note speed control. Stock VoicePlayer never touches
// AudioBufferSourceNode.playbackRate, so voice notes always play at 1x. This
// patch adds a 1x / 1.5x / 2x toggle and keeps the waveform/progress/auto-stop
// math correct by scaling the single source of elapsed time (getPlayedTime).
const SPEEDS = [1, 1.5, 2];
const STORAGE_KEY = "owa.voicePlaybackSpeed";

function readStoredSpeed() {
    try {
        const val = parseFloat(browser.sessionStorage?.getItem(STORAGE_KEY));
        return SPEEDS.includes(val) ? val : 1;
    } catch {
        // sessionStorage can be unavailable (private mode / sandboxed iframe)
        return 1;
    }
}

patch(VoicePlayer.prototype, {
    setup() {
        super.setup();
        // Add the reactive key before first render so the template binds to it.
        this.state.playbackSpeed = readStoredSpeed();
    },

    createSource() {
        super.createSource();
        if (this.source) {
            try {
                this.source.playbackRate.value = this.state.playbackSpeed || 1;
            } catch {
                // playbackRate unsupported on this browser — plays at 1x.
            }
        }
    },

    // Buffer-time elapsed = real-time elapsed * rate. Every time/progress
    // calculation in the base class derives from getPlayedTime(), so scaling
    // this one method keeps the progress bar, visual timer, and the
    // `scheduledPause >= buffer.duration` auto-stop consistent at any speed.
    getPlayedTime() {
        return super.getPlayedTime() * (this.state.playbackSpeed || 1);
    },

    cyclePlaybackSpeed() {
        const idx = SPEEDS.indexOf(this.state.playbackSpeed);
        this.setPlaybackSpeed(SPEEDS[(idx + 1) % SPEEDS.length]);
    },

    setPlaybackSpeed(rate) {
        // Restart from the current position so the elapsed-time math never
        // mixes two rates within one playback.
        const resume = this.state.playing && !this.state.paused;
        if (resume) {
            this.pause();
        }
        this.state.playbackSpeed = rate;
        try {
            browser.sessionStorage?.setItem(STORAGE_KEY, String(rate));
        } catch {
            // non-fatal: just don't persist the preference
        }
        if (resume) {
            this.play();
        }
    },
});
