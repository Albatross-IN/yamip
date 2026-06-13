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
    
    const inputPhone = document.getElementById('otp_phone');
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
        const phoneVal = inputPhone.value.trim();
        if (!phoneVal) {
            showError('Please enter a phone number.');
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
        const phoneVal = inputPhone.value.trim();
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
    inputPhone.addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (containerCode.classList.contains('d-none')) {
                sendOtpRequest();
            }
        }
    });

    inputCode.addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (!containerCode.classList.contains('d-none')) {
                btnVerifyOtp.click();
            }
        }
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initOtpLogin);
} else {
    initOtpLogin();
}
