var exec = require('child_process').exec;

module.exports.calculateExif = function(file) {
  return new Promise(
    function(resolve, reject) {
      var cmd = 'exiftool -json "' + file + '"';
      exec(cmd, function(error, stdout, stderr) {
        if (error) {
          return reject(error);
        }
        try {
          return resolve(JSON.parse(stdout)[0]);
        } catch (e) {
          return reject(e);
        }
      });
    }
  );
};
