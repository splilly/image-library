var crypto = require('crypto');
var fs = require('fs');

// change the algo to sha1, sha256 etc according to your requirements
var algo = 'sha1';

module.exports.calculateHash = function(file) {
  return new Promise(
    function(resolve, reject) {
      console.log("calculateHash('" + file + "')");
      var shasum = crypto.createHash(algo);
      var s = fs.ReadStream(file);
      s.on('error', function(error) { reject(error); });
      s.on('data', function(d) { shasum.update(d); });
      s.on('end', function() {
        var d = shasum.digest('hex');
        resolve(d);
      });
    }
  );
};
