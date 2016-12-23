var ProgressBar = require('progress');
var fs = require('../libs/fs');
var hash = require('../libs/hash');
var db = require('../libs/db');
var exif = require('../libs/exif');



var calculateHash = function(filename) {
  return new Promise((resolve, reject) => {
    hash.calculateHash(filename)
    .then((hashValue) => {
      resolve({filename: filename, hashValue: hashValue});
    })
    .catch((reason) => {
      reject(reason);
    });
  });
};

var fetchDbRecordForHash = function(sofar) {
  return new Promise((resolve, reject) => {
    db.fetchForHash(sofar.hashValue)
    .then((dbRecord) => {
      if (!dbRecord) {
        sofar.saveRequired = true;
      }
      sofar.dbRecord = dbRecord || {filenames:[sofar.filename], hash: sofar.hashValue};
      resolve(sofar);
    })
    .catch((reason) => {
      reject(reason);
    });
  });
};

var maybeAddFilenameToDbRecord = function(sofar) {
  return new Promise((resolve, reject) => {
    if (sofar.dbRecord.filenames.indexOf(sofar.filename) === -1) {
      sofar.dbRecord.filenames.push(sofar.filename);
      sofar.saveRequired = true;
    }
    resolve(sofar);
  });
};

var maybeAddExifToDbRecord = function(sofar) {
  return new Promise((resolve, reject) => {
    if (!sofar.dbRecord.exif) {
      exif.calculateExif(sofar.filename)
      .then((exifValue) => {
        sofar.dbRecord.exif = exifValue;
        sofar.saveRequired = true;
        resolve(sofar);
      })
      .catch((reason) => {
        reject(reason);
      });
    } else {
      resolve(sofar);
    }
  });
};

var maybeStoreDbRecordForHash = function(sofar) {
  return new Promise((resolve, reject) => {
    if (sofar.saveRequired) {
      db.save(sofar.dbRecord)
      .then((dbRecord) => {
        resolve(sofar);
      })
      .catch((reason) => {
        reject(reason);
      });
    } else {
      resolve(sofar);
    }
  });
};

var processFile = function(filename) {
  return new Promise((resolve, reject) => {
    // calculate hash
    calculateHash(filename)
    // get db record for hash
    .then(fetchDbRecordForHash)
    // add filename to db record if not already contained
    .then(maybeAddFilenameToDbRecord)
    // add exif to db record if not already conatined
    .then(maybeAddExifToDbRecord)
    // if added filename or exif store db record
    .then(maybeStoreDbRecordForHash)
    // log that we did something
    .then(() => {
      console.log("Processed file " + filename);
      resolve(filename);
    })
    // handle errors if they happen
    .catch(function(reason) {
      reject(reason);
    });
  });
}

exports.execute = function(inputDir, outputDir, dryrun, callback) {
  var count = 0;

  function processNextFile(iterator) {
    var record = iterator.next();
    if (record.value) {
      console.log("Found " + ++count + " \t->" + record.value);
      processFile(record.value)
      .then((result) => {
        if (!record.done) {
          processNextFile(iterator);
        }
      });
    } else {
      db.cleanup()
      .catch((reason) => {
        console.error(reason);
      })
    }
  }

  db.init()
  .then(() => {
    var it = fs.findFilesInDir(inputDir, (f) => { return (/^.*\.(jpg|jpeg|cr2|crw|tif|tiff|avi|mpg|mpeg)$/i).test(f)});
    processNextFile(it);
  })
};
// exports.execute = function(inputDir, outputDir, dryrun, callback) {
//   var sequence = Promise.resolve();
//   var it = fs.findFilesInDir(inputDir, (f) => { return (/^.*\.(jpg|jpeg|cr2|crw|tif|tiff|avi|mpg|mpeg)$/i).test(f)});
//   var count = 0;
//   for (var filename of it) {
//     console.log("Found " + ++count + " \t->" + filename);
//     if (count < 100) {
//       sequence = sequence.then(processFile(filename))
//       .catch((reason) => {
//         console.error(reason);
//       });
//     }
//   }
// };
