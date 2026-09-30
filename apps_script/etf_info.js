// etf-info — GAS 알람: 평일 아침 GitHub 워크플로를 workflow_dispatch 로 깨운다.
var GH_OWNER = 'muhwa91';
// 레포명은 소스에 두지 않는다(공개 미러 유출) → Script Properties 의 GH_REPO. README 3번 참고.
var GH_WORKFLOW = 'etf_simulator.yml';
var GH_REF = 'main';
var TZ = 'Asia/Seoul';
var WINDOW_FROM = 820;  // 08:20
var WINDOW_TO = 832;    // 08:32

function tickDispatchEtf() {
  var now = new Date();
  var dow = parseInt(Utilities.formatDate(now, TZ, 'u'), 10);   // 1=월 .. 7=일
  var hm = parseInt(Utilities.formatDate(now, TZ, 'HHmm'), 10);
  var today = Utilities.formatDate(now, TZ, 'yyyy-MM-dd');
  if (dow > 5) return;
  if (hm < WINDOW_FROM || hm > WINDOW_TO) return;
  var props = PropertiesService.getScriptProperties();
  if (props.getProperty('dispatched_date') === today) return;
  if (dispatchGithub_()) {
    props.setProperty('dispatched_date', today);
    Logger.log('dispatched ' + today + ' hm=' + hm);
  }
}

// 설정 누락은 조용히 넘기지 않고 던진다 — return 하면 매일 아침 디스패치가 죽어도 아무도 모른다.
// 던지면 GAS 실행 로그에 남고 트리거 실패 알림 메일이 온다.
function requiredProp_(name) {
  var v = PropertiesService.getScriptProperties().getProperty(name);
  if (!v) {
    throw new Error('Script Properties 에 ' + name + ' 가 없습니다. ' +
      'GAS 편집기 → 프로젝트 설정 → 스크립트 속성 → 속성 추가 로 등록하세요.');
  }
  return v;
}

function dispatchGithub_() {
  var token = requiredProp_('GH_TOKEN');
  var url = 'https://api.github.com/repos/' + GH_OWNER + '/' + requiredProp_('GH_REPO') +
            '/actions/workflows/' + GH_WORKFLOW + '/dispatches';
  var res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ ref: GH_REF }),
    muteHttpExceptions: true
  });
  var code = res.getResponseCode();
  if (code !== 204) Logger.log('GitHub 응답 ' + code + ': ' + res.getContentText());
  return code === 204;
}

function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'tickDispatchEtf') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('tickDispatchEtf').timeBased().everyMinutes(5).create();
  Logger.log('트리거 등록됨(매 5분)');
}

function testDispatchNow() {
  Logger.log(dispatchGithub_() ? '테스트 성공(204)' : '테스트 실패');
}