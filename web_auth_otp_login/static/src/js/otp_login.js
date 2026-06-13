// Country calling codes mapping based on ISO 2-letter country codes
const countryToPrefix = {
    'US': '+1', 'CA': '+1', 'IN': '+91', 'GB': '+44', 'AU': '+61', 
    'NZ': '+64', 'SG': '+65', 'AE': '+971', 'SA': '+966', 'ZA': '+27',
    'DE': '+49', 'FR': '+33', 'IT': '+39', 'ES': '+34', 'NL': '+31',
    'BE': '+32', 'CH': '+41', 'AT': '+43', 'PK': '+92', 'BD': '+880',
    'LK': '+94', 'NP': '+977', 'MY': '+60', 'ID': '+62', 'TH': '+66',
    'PH': '+63', 'VN': '+84', 'HK': '+852', 'TW': '+886', 'JP': '+81',
    'KR': '+82', 'CN': '+86', 'BR': '+55', 'MX': '+52', 'AR': '+54',
    'CL': '+56', 'CO': '+57', 'PE': '+51', 'VE': '+58', 'RU': '+7',
    'TR': '+90', 'UA': '+380', 'PL': '+48', 'RO': '+40', 'EG': '+20',
    'NG': '+234', 'KE': '+254', 'GH': '+233', 'MA': '+212'
};

// Helper to wrap fetch with a timeout
function fetchWithTimeout(url, options = {}, timeout = 1500) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Timeout')), timeout);
        fetch(url, options)
            .then(response => {
                clearTimeout(timer);
                resolve(response);
            })
            .catch(err => {
                clearTimeout(timer);
                reject(err);
            });
    });
}

// Automatically detect country prefix based on IP, browser language, or timezone
async function detectCountryPrefix() {
    // 1. Try freeipapi.com Geolocation API (higher rate limits, no keys needed)
    try {
        const response = await fetchWithTimeout('https://freeipapi.com/api/json');
        if (response.ok) {
            const data = await response.json();
            if (data && data.countryCode) {
                const prefix = countryToPrefix[data.countryCode.toUpperCase()];
                if (prefix) return prefix;
            }
        }
    } catch (e) {
        console.warn("freeipapi Geolocation failed, trying backup API...", e);
    }

    // 2. Try ipinfo.io Geolocation API as backup
    try {
        const response = await fetchWithTimeout('https://ipinfo.io/json');
        if (response.ok) {
            const data = await response.json();
            if (data && data.country) {
                const prefix = countryToPrefix[data.country.toUpperCase()];
                if (prefix) return prefix;
            }
        }
    } catch (e) {
        console.warn("Backup Geolocation failed, checking timezone...", e);
    }

    // 3. Fallback: Timezone mapping (More accurate for physical location than browser language)
    try {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (tz) {
            const tzLower = tz.toLowerCase();
            if (tzLower.includes('kolkata') || tzLower.includes('calcutta')) return '+91';
            if (tzLower.includes('london')) return '+44';
            if (tzLower.includes('singapore')) return '+65';
            if (tzLower.includes('dubai')) return '+971';
            if (tzLower.includes('sydney') || tzLower.includes('melbourne') || tzLower.includes('brisbane')) return '+61';
            if (tzLower.includes('tokyo')) return '+81';
            if (tzLower.includes('seoul')) return '+82';
            if (tzLower.includes('hong_kong')) return '+852';
            if (tzLower.includes('taipei')) return '+886';
            if (tzLower.includes('shanghai') || tzLower.includes('urumqi')) return '+86';
            if (tzLower.includes('dhaka')) return '+880';
            if (tzLower.includes('karachi')) return '+92';
            if (tzLower.includes('colombo')) return '+94';
            if (tzLower.includes('kathmandu')) return '+977';
            if (tzLower.includes('johannesburg')) return '+27';
            if (tzLower.includes('riyadh')) return '+966';
            if (tzLower.includes('istanbul')) return '+90';
            if (tzLower.includes('sao_paulo')) return '+55';
            if (tzLower.includes('mexico_city')) return '+52';
            if (tzLower.includes('buenos_aires')) return '+54';
            if (tzLower.includes('moscow')) return '+7';
            if (tzLower.includes('america/')) return '+1';
        }
    } catch (e) {
        console.warn("Timezone detection failed", e);
    }

    // 4. Fallback: Browser language/locale region code (last-resort fallback)
    const lang = navigator.language || navigator.userLanguage;
    if (lang && lang.includes('-')) {
        const country = lang.split('-')[1].toUpperCase();
        if (countryToPrefix[country]) {
            return countryToPrefix[country];
        }
    }

    // 5. Default Fallback
    return '+91';
}

function initOtpLogin() {
    const otpLoginForm = document.getElementById('otp_login_form');
    if (!otpLoginForm) {
        return; // Only run on pages containing the OTP login form
    }

    const emailForm = document.querySelector('.oe_login_form');
    const tabEmail = document.getElementById('tab_email');
    const tabOtp = document.getElementById('tab_otp');

    const btnSendOtp = document.getElementById('btn_send_otp');
    const btnVerifyOtp = document.getElementById('btn_verify_otp');
    const btnResendOtp = document.getElementById('btn_resend_otp');
    
    const inputCountryCode = document.getElementById('otp_country_code');
    const inputPhoneNum = document.getElementById('otp_phone_num');
    const inputCode = document.getElementById('otp_code');
    
    const containerCode = document.getElementById('otp_code_container');
    const containerResend = document.getElementById('resend_container');
    const timerSpan = document.getElementById('otp_timer');
    
    const errorMsg = document.getElementById('otp_error_msg');
    const successMsg = document.getElementById('otp_success_msg');

    let timerInterval = null;

    // Helper functions
    function showError(message) {
        errorMsg.textContent = message;
        errorMsg.classList.remove('d-none');
        successMsg.classList.add('d-none');
    }

    function showSuccess(message) {
        successMsg.textContent = message;
        successMsg.classList.remove('d-none');
        errorMsg.classList.add('d-none');
    }

    function clearMessages() {
        errorMsg.classList.add('d-none');
        successMsg.classList.add('d-none');
    }

    // Detect and set country code prefix on load
    detectCountryPrefix().then(prefix => {
        if (inputCountryCode && prefix) {
            inputCountryCode.value = prefix;
        }
    });

    // Helper to get formatted full phone number
    function getFullPhoneNumber() {
        let countryCode = inputCountryCode.value.trim();
        let phoneNum = inputPhoneNum.value.trim();

        if (!phoneNum) {
            return '';
        }

        // Format country code to start with '+'
        if (countryCode && !countryCode.startsWith('+')) {
            countryCode = '+' + countryCode;
        }

        // Remove spaces, hyphens, and parenthesis from local number
        const cleanPhoneNum = phoneNum.replace(/[\s\-\(\)]/g, '');
        return countryCode + cleanPhoneNum;
    }

    // Toggle Tab Behavior
    tabEmail.addEventListener('click', function () {
        tabOtp.classList.remove('active');
        tabEmail.classList.add('active');
        otpLoginForm.classList.add('d-none');
        if (emailForm) {
            emailForm.classList.remove('d-none');
        }
        clearMessages();
    });

    tabOtp.addEventListener('click', function () {
        tabEmail.classList.remove('active');
        tabOtp.classList.add('active');
        if (emailForm) {
            emailForm.classList.add('d-none');
        }
        otpLoginForm.classList.remove('d-none');
        clearMessages();
    });

    // Handle OTP Sending
    function sendOtpRequest() {
        const phoneVal = getFullPhoneNumber();
        if (!phoneVal) {
            showError('Please enter your phone number.');
            return;
        }

        clearMessages();
        btnSendOtp.disabled = true;
        btnSendOtp.textContent = 'Sending...';

        fetch('/web/auth/otp/send', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                jsonrpc: '2.0',
                method: 'call',
                params: {
                    phone: phoneVal
                }
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showError(data.error.data ? data.error.data.message : 'An error occurred on the server.');
                resetSendButton();
                return;
            }

            const result = data.result;
            if (result && result.success) {
                showSuccess(result.message);
                
                // Show Verification Fields
                containerCode.classList.remove('d-none');
                btnVerifyOtp.classList.remove('d-none');
                containerResend.classList.remove('d-none');
                btnSendOtp.classList.add('d-none');
                
                inputCode.focus();
                startTimer(60);
            } else {
                showError(result ? result.error : 'Failed to send OTP. Please check the number.');
                resetSendButton();
            }
        })
        .catch(err => {
            console.error('Error sending OTP:', err);
            showError('Connection error. Please try again.');
            resetSendButton();
        });
    }

    function resetSendButton() {
        btnSendOtp.disabled = false;
        btnSendOtp.textContent = 'Send OTP via WhatsApp';
    }

    btnSendOtp.addEventListener('click', sendOtpRequest);
    btnResendOtp.addEventListener('click', sendOtpRequest);

    // Resend Code Timer
    function startTimer(duration) {
        clearInterval(timerInterval);
        btnResendOtp.disabled = true;
        btnResendOtp.classList.add('text-muted');
        
        let secondsLeft = duration;
        timerSpan.textContent = `Resend in ${secondsLeft}s`;

        timerInterval = setInterval(function () {
            secondsLeft--;
            if (secondsLeft <= 0) {
                clearInterval(timerInterval);
                timerSpan.textContent = '';
                btnResendOtp.disabled = false;
                btnResendOtp.classList.remove('text-muted');
            } else {
                timerSpan.textContent = `Resend in ${secondsLeft}s`;
            }
        }, 1000);
    }

    // Handle OTP Verification
    btnVerifyOtp.addEventListener('click', function () {
        const phoneVal = getFullPhoneNumber();
        const codeVal = inputCode.value.trim();

        if (!phoneVal || !codeVal) {
            showError('Please fill in both phone and OTP fields.');
            return;
        }

        if (codeVal.length !== 6 || isNaN(codeVal)) {
            showError('Please enter a valid 6-digit code.');
            return;
        }

        clearMessages();
        btnVerifyOtp.disabled = true;
        btnVerifyOtp.textContent = 'Verifying...';

        // Extract redirect from current URL query
        const urlParams = new URLSearchParams(window.location.search);
        const redirect = urlParams.get('redirect') || '/shop/checkout';

        fetch('/web/auth/otp/verify', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                jsonrpc: '2.0',
                method: 'call',
                params: {
                    phone: phoneVal,
                    otp_code: codeVal,
                    redirect: redirect
                }
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showError(data.error.data ? data.error.data.message : 'An error occurred during verification.');
                resetVerifyButton();
                return;
            }

            const result = data.result;
            if (result && result.success) {
                showSuccess(result.message + ' Redirecting...');
                clearInterval(timerInterval);
                setTimeout(function () {
                    window.location.href = result.redirect || '/shop/checkout';
                }, 1000);
            } else {
                showError(result ? result.error : 'Incorrect verification code.');
                resetVerifyButton();
            }
        })
        .catch(err => {
            console.error('Error verifying OTP:', err);
            showError('Connection error. Please try again.');
            resetVerifyButton();
        });
    });

    function resetVerifyButton() {
        btnVerifyOtp.disabled = false;
        btnVerifyOtp.textContent = 'Verify & Sign In';
    }

    // Form helper to submit using Enter key
    const triggerSubmit = function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (containerCode.classList.contains('d-none')) {
                sendOtpRequest();
            }
        }
    };

    inputCountryCode.addEventListener('keypress', triggerSubmit);
    inputPhoneNum.addEventListener('keypress', triggerSubmit);

    inputCode.addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (!containerCode.classList.contains('d-none')) {
                btnVerifyOtp.click();
            }
        }
    });

    // Auto submit OTP once 6 digits are typed
    inputCode.addEventListener('input', function () {
        const val = inputCode.value.trim();
        if (val.length === 6 && !isNaN(val)) {
            btnVerifyOtp.click();
        }
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initOtpLogin);
} else {
    initOtpLogin();
}
