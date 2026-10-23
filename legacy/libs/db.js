var MongoClient = require('mongodb').MongoClient;
var url = 'mongodb://localhost:27017/image-library';
var db;
var collection;

var init = function() {
  return MongoClient.connect(url)
  .then((result) => {
    db = result;
    collection = db.collection('documents');
    return result;
  });
}

var cleanup = function() {
  return db.close();
}

var saveMongoDb = function(record) {
  return new Promise((resolve, reject) => {
    // MongoClient.connect(url)
    // .then(function(db) {
    //   // Get the documents collection
    //   var collection = db.collection('documents');
      // Insert the documents
      collection.updateOne({hash:record.hash}, record, {upsert: true})
      .then(function(value) {
        // db.close();
        resolve(value);
      })
      .catch(function(reason) {
        reject(reason);
      });
    //   // Close the db connection
    //   db.close();
    // })
    // .catch(function(reason) {
    //   reject(reason);
    // });
  });
}

var fetchForHashMongoDb = function(hash) {
  return new Promise(function(resolve, reject) {
    // MongoClient.connect(url)
    // .then(function(db) {
    //   // Get the documents collection
    //   var collection = db.collection('documents');
      // Query the document for the given hash
      collection.findOne({hash:hash})
      .then(function(result) {
        resolve(result);
      })
      .catch(function(reason) {
        reject(reason);
      });
    //   // Close the db connection
    //   db.close();
    // })
    // .catch(function(reason) {
    //   reject(reason);
    // });
  });
}

module.exports.init = init;
module.exports.cleanup = cleanup;
module.exports.fetchForHash = fetchForHashMongoDb;
module.exports.save = saveMongoDb;
