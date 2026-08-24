// 심사용 로그인. 성공하면 서버가 세션 쿠키를 내려주므로 첫 화면으로 보내면 된다.
const form = document.getElementById("review-form");
const error = document.getElementById("review-error");
const submit = document.getElementById("review-submit");

function showError(message) {
  error.textContent = message;
  error.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  submit.textContent = "확인 중...";
  try {
    const res = await fetch("/api/review-login", {
      method: "POST",
      body: new FormData(form),
    });
    if (res.ok) {
      window.location.href = "/";
      return;
    }
    let message = "로그인에 실패했습니다.";
    try {
      const data = await res.json();
      if (data.error) message = data.error;
      else if (data.detail) message = data.detail;
    } catch {
      /* 본문이 JSON이 아니면 기본 문구를 쓴다 */
    }
    showError(message);
  } catch {
    showError("네트워크 오류로 로그인하지 못했습니다.");
  } finally {
    submit.disabled = false;
    submit.textContent = "로그인";
  }
});
