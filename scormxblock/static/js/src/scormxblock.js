function ScormXBlock(runtime, element, settings) {

  function SCORM_12_API(){

    this.LMSInitialize = function(){
      console.log('LMSInitialize');
      lastError = 0;
      return 'true';
    };

    this.LMSFinish = function() {
      console.log('LMSFinish');
      return StoreValues();
    };

    this.LMSGetValue = GetValue;
    this.LMSSetValue = SetValue;

    this.LMSCommit = function() {
      console.log('LMSCommit');
      return StoreValues();
    };

    this.LMSGetLastError = function() {
      console.log('GetLastError');
      return lastError;
    };

    this.LMSGetErrorString = function(errorCode) {
      console.log('LMSGetErrorString');
      return SCORM_12_ERRORS[errorCode.toString()];
    };

    this.LMSGetDiagnostic = function(errorCode) {
      console.log('LMSGetDiagnostic');
      return 'Some Diagnostice';
    }
  }

  function SCORM_2004_API(){
    this.Initialize = function(){
      console.log('LMSInitialize');
      return 'true';
    };

    this.Terminate = function() {
      console.log('LMSFinish');
      return StoreValues();
    };

    this.GetValue = GetValue;
    this.SetValue = SetValue;

    this.Commit = function() {
      console.log('LMSCommit')
      return StoreValues();
    };

    this.GetLastError = function() {
      console.log('GetLastError');
      return '0';
    };

    this.GetErrorString = function(errorCode) {
      console.log('LMSGetErrorString');
      return 'Some Error';
    };

    this.GetDiagnostic = function(errorCode) {
      console.log('LMSGetDiagnostic');
      return 'Some Diagnostice';
    }
  }

  var lastError = 301;

  var SCORM_12_ERRORS = {
    '0': 'No Error',
    '101': 'General Exception',
    '201': 'Invalid argument error',
    '202': 'Element cannot have children',
    '203': 'Element not an array. Cannot have count',
    '301': 'Not initialized',
    '401': 'Not implemented error',
    '402': 'Invalid set value, element is a keyword',
    '403': 'Element is read only',
    '404': 'Element is write only',
    '405': 'Incorrect Data Type',
  };

  var values = [];
  var LMSValues = {};

  var GetValue = function (cmi_element) {

    if (settings.valid_read_data.includes(cmi_element)){
      var handlerUrl = runtime.handlerUrl(element, 'scorm_get_values');

      if (values.length===0) {
        var response = $.ajax({
          type: 'GET',
          url: handlerUrl,
          async: false,
        });
        values = JSON.parse(response.responseText);
      }

      var value = values[cmi_element];

      if (value==undefined){
        value = '';
      }

      console.log('Getvalue for ' + cmi_element + ' = ' + value);
      return value
    } else if (settings.valid_write_data.includes(cmi_element)) {
      lastError = 404;
    } else if (cmi_element.endsWith('_count')){
      lastError = 203;
    } else if (cmi_element.endsWith('_children')){
      lastError = 202;
    } else {
      lastError = 401;
    }
    return '';

  };

  var SetValue = function (cmi_element, value) {

    if (value == null || value == 'undefined'){
      lastError = 405;
    } else if (settings.valid_write_data.includes(cmi_element)) {
      console.log('LMSSetValue ' + cmi_element + ' = ' + value);
      LMSValues[cmi_element] = value
      return 'true';
    } else if (cmi_element.endsWith('_count') || cmi_element.endsWith('_children')){
      lastError = 402;
    } else if (settings.valid_read_data.includes(cmi_element)){
      lastError = 403;
    } else {
      lastError = 401;
    }
    return 'false';

  };

  var StoreValues = function () {
    var handlerUrl = runtime.handlerUrl( element, 'scorm_set_values');

    $.ajax({
      type: 'POST',
      url: handlerUrl,
      data: JSON.stringify(LMSValues),
      async: false,
      success: function(response){
        if (typeof response.lesson_score != 'undefined'){
          $('.lesson_score', element).html(response.lesson_score);
        }
        $('.completion_status', element).html(response.completion_status);
        LMSValues = {};
      }
    });

    return 'true';
  };

  $(function ($) {
    if (settings.version_scorm == 'SCORM_12') {
      API = new SCORM_12_API();
    } else {
      API_1484_11 = new SCORM_2004_API();
    }
    console.log('Initial SCORM data...');
  });
}
